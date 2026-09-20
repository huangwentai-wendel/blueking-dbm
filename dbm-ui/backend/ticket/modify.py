# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.
Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at https://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import copy
import logging

from django.db import transaction
from django.db.models import F
from django.utils.translation import gettext as _

from backend.configuration.models import DBAdministrator
from backend.ticket.builders import BuilderFactory
from backend.ticket.constants import (
    FlowContext,
    FlowType,
    OperateNodeActionType,
    TicketFlowStatus,
    TicketModifyType,
    TicketStatus,
    TodoStatus,
    TodoType,
)
from backend.ticket.exceptions import (
    TicketContentChangedException,
    TicketContentVersionMissingException,
    TicketModifyForbiddenException,
    TicketModifyStatusException,
)
from backend.ticket.models import Flow, Ticket, TicketModifyRecord

logger = logging.getLogger("root")


class TicketModifyHandler:
    """
    单据改单服务
    三种模式共用同一份事务骨架：
      - 代为修改(ON_BEHALF)：审批人在待审批阶段，拒绝外部 ITSM 单据后转入待确认修改
      - 重新编辑(RE_EDIT)：提单人在待审批/待确认修改阶段重新编辑内容(待审批阶段支持多次编辑并同步更新 ITSM 单据)
      - 调整申请(ADJUST_APPLY)：处理人在待补货阶段，调整资源申请条件并重新申领

    改单后的 flow 操作规则：
      - 已结束的 flow 冻结为 SKIPPED + 出口词，不删除
      - 未执行的 PENDING 尾巴用新的 ticket.details 覆盖(不删除、不重建 id)
      - 需要插入的新节点(确认修改 / 新一轮审批)按 order 插入，并对后续节点做 order shift
    """

    @classmethod
    def modify(cls, ticket_id, operator, mode, details, remark="", version=None, change_count=0):
        """改单统一入口，事务内完成乐观锁/权限/状态校验并分发到具体模式"""
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(id=ticket_id)

            # 内容版本乐观锁：version 由前端随提交传入，当前版本 = 已落库的改单记录数
            if version is not None and cls._current_version(ticket) != version:
                raise TicketContentChangedException()

            if mode == TicketModifyType.ON_BEHALF.value:
                cls.on_behalf_modify(ticket, operator, details, remark, change_count)
            elif mode == TicketModifyType.RE_EDIT.value:
                cls.re_edit(ticket, operator, details, remark, change_count)
            elif mode == TicketModifyType.ADJUST_APPLY.value:
                cls.adjust_apply(ticket, operator, details, remark, change_count)
            else:
                raise TicketModifyForbiddenException()

            # 状态流转在 TicketFlowManager.update_ticket_status 中通过新的 ticket 实例落库，
            # 这里刷新本地实例的状态，保证返回给前端/上层调用方的 ticket.status 与 DB 一致
            ticket.refresh_from_db(fields=["status"])
            return ticket

    @staticmethod
    def _current_version(ticket):
        """当前内容版本号 = 已落库的改单记录数。

        Ticket 表数据量大，不为其新增字段；每次改单都会落一条 TicketModifyRecord，
        记录数即版本号，前端提交的 version 与此值比对即可实现乐观锁。
        """
        return ticket.modify_records.count()

    @staticmethod
    def check_content_version(ticket, version):
        """审批通过/拒绝时校验内容版本，version 必传。

        版本号与改单提交的乐观锁同源（改单记录数），待审批阶段提单人每次重新编辑都会落一条
        RE_EDIT 记录使版本递增，审批人持旧版本操作即被拦截。
        审批通过/拒绝属终态操作，缺少 version 说明前端未带上版本号，直接拒绝，避免漏校验放行。
        """
        if version is None:
            raise TicketContentVersionMissingException()
        if TicketModifyHandler._current_version(ticket) != version:
            raise TicketContentChangedException()

    @staticmethod
    def _notify_creator(ticket, operator, mode, remark):
        """改单后通知提单人：异步发送，事务提交后再投递，避免读到未提交数据"""
        from backend.core import notify

        transaction.on_commit(
            lambda: notify.send_modify_notify.apply_async(args=(ticket.id, operator, mode.value, remark))
        )

    @staticmethod
    def _notify_approvers(ticket):
        """重新编辑后通知审批人：复用现网「进入待审批」通知，不新增通知类型"""
        from backend.core import notify

        transaction.on_commit(lambda: notify.send_msg.apply_async(args=(ticket.id,)))

    # ------------------------------------------------------------------
    # 代为修改：审批人 -> 拒绝外部 ITSM -> 待确认修改
    # ------------------------------------------------------------------
    @classmethod
    def on_behalf_modify(cls, ticket, operator, details, remark, change_count):
        cls._check_approve_permission(ticket, operator)
        if ticket.status != TicketStatus.APPROVE:
            raise TicketModifyStatusException(status=TicketStatus.get_choice_label(ticket.status))

        itsm_flow = cls._get_running_itsm_flow(ticket)

        # 拒绝外部 ITSM 单据(审批人终止即拒单)，而非撤销
        from backend.ticket.handler import TicketHandler

        TicketHandler.operate_itsm_ticket(
            ticket.id,
            action=OperateNodeActionType.TRANSITION,
            operator=operator,
            is_approved=False,
            action_message=remark or _("审批人代为修改"),
        )

        # 冻结审批节点并关闭其待办
        cls._freeze_flow(itsm_flow, exit_word=_("代为修改"), remark=remark)
        cls._close_flow_todos(itsm_flow, operator)

        # 覆盖详情并重新初始化派生字段(改前快照在覆盖前捕获，覆盖并 patch 后的详情才是 after)
        before_details = copy.deepcopy(ticket.details)
        builder = cls._patch_details(ticket, details)
        cls._record(
            ticket,
            itsm_flow,
            TicketModifyType.ON_BEHALF,
            operator,
            remark,
            ticket.details,
            change_count,
            before_details,
        )

        # 插入确认修改节点并覆盖 pending 尾巴
        auto_confirm = operator == ticket.creator
        cls._insert_confirm_modify_and_rebuild_tail(ticket, itsm_flow, auto_confirm=auto_confirm, builder=builder)

        from backend.ticket.flow_manager.manager import TicketFlowManager

        if auto_confirm:
            # 提单人自身具备审批权限时的代为修改：提交即同步完成确认，不产待办/通知
            cls._auto_confirm_modify(ticket)
            TicketFlowManager(ticket=ticket).run_next_flow()
        else:
            # 运行确认修改节点：创建待确认修改待办，post_save 信号会把 status 置为 CONFIRM_PENDING
            TicketFlowManager(ticket=ticket).run_next_flow()
            # 通知提单人：内容已被审批人代为修改
            cls._notify_creator(ticket, operator, TicketModifyType.ON_BEHALF, remark)

    # ------------------------------------------------------------------
    # 重新编辑：提单人在待审批/待确认修改阶段重新编辑内容
    # ------------------------------------------------------------------
    @classmethod
    def re_edit(cls, ticket, operator, details, remark, change_count):
        if operator != ticket.creator:
            raise TicketModifyForbiddenException()
        if ticket.status == TicketStatus.CONFIRM_PENDING:
            cls._re_edit_from_confirm(ticket, operator, details, remark, change_count)
        elif ticket.status == TicketStatus.APPROVE:
            cls._re_edit_from_approve(ticket, operator, details, remark, change_count)
        else:
            raise TicketModifyStatusException(status=TicketStatus.get_choice_label(ticket.status))

    @classmethod
    def _re_edit_from_confirm(cls, ticket, operator, details, remark, change_count):
        """待确认修改阶段的重新编辑：结束确认修改节点，按需重审后重新执行"""
        confirm_flow = ticket.flows.filter(flow_type=FlowType.CONFIRM_MODIFY.value).order_by("order", "id").last()

        # 冻结确认修改节点并关闭其待办
        cls._freeze_flow(confirm_flow, exit_word=_("重新编辑"), remark=remark)
        cls._close_flow_todos(confirm_flow, operator)

        # 覆盖详情并重新初始化派生字段(改前快照在覆盖前捕获)
        before_details = copy.deepcopy(ticket.details)
        builder = cls._patch_details(ticket, details)
        cls._record(
            ticket,
            confirm_flow,
            TicketModifyType.RE_EDIT,
            operator,
            remark,
            ticket.details,
            change_count,
            before_details,
        )

        insert_order = confirm_flow.order + 1

        if builder.need_itsm:
            # 需要重审：插入新一轮审批节点，覆盖尾巴
            cls._shift_orders(ticket, insert_order)
            itsm_flow = builder.build_itsm_flow()
            itsm_flow.order = insert_order
            Flow.objects.bulk_create([itsm_flow])
            cls._overwrite_pending_tail(ticket, builder, insert_order + 1)
        else:
            # 免审批：直接覆盖尾巴并执行
            cls._overwrite_pending_tail(ticket, builder, insert_order)

        from backend.ticket.flow_manager.manager import TicketFlowManager

        TicketFlowManager(ticket=ticket).run_next_flow()

    @classmethod
    def _re_edit_from_approve(cls, ticket, operator, details, remark, change_count):
        """待审批阶段的重新编辑：更新外部 ITSM 单据内容，单据仍停待审批，支持多次编辑"""
        itsm_flow = cls._get_running_itsm_flow(ticket)

        # 覆盖详情并重新初始化派生字段(改前快照在覆盖前捕获，覆盖并 patch 后的详情才是 after)
        before_details = copy.deepcopy(ticket.details)
        builder = cls._patch_details(ticket, details)
        cls._record(
            ticket, itsm_flow, TicketModifyType.RE_EDIT, operator, remark, ticket.details, change_count, before_details
        )

        # 改后内容命中免审批策略：撤销外部 ITSM 单据，审批节点以「免审批」出口结束，直达待执行
        if not builder.need_itsm:
            from backend.ticket.handler import TicketHandler

            TicketHandler.operate_itsm_ticket(ticket.id, action=OperateNodeActionType.WITHDRAW, operator=operator)
            cls._freeze_flow(itsm_flow, exit_word=_("免审批"))
            cls._close_flow_todos(itsm_flow, operator)
            cls._overwrite_pending_tail(ticket, builder, itsm_flow.order + 1)

            from backend.ticket.flow_manager.manager import TicketFlowManager

            TicketFlowManager(ticket=ticket).run_next_flow()
            return

        # 仍需要审批：更新外部 ITSM 单据(V4 handle_ticket update，幂等可多次调用)
        cls._update_itsm_ticket(ticket, itsm_flow, operator)

        # 用新内容覆盖 pending 尾巴(不冻结、不插节点、不 shift，单据仍待审批)
        cls._overwrite_pending_tail(ticket, builder, itsm_flow.order + 1)

        # 通知审批人：内容已被重新编辑(复用现网「进入待审批」通知)
        cls._notify_approvers(ticket)

    # ------------------------------------------------------------------
    # 调整申请：待补货阶段调整资源申请条件并重新申领
    # ------------------------------------------------------------------
    @classmethod
    def adjust_apply(cls, ticket, operator, details, remark, change_count):
        cls._check_replenish_permission(ticket, operator)
        if ticket.status != TicketStatus.RESOURCE_REPLENISH:
            raise TicketModifyStatusException(status=TicketStatus.get_choice_label(ticket.status))

        resource_flow = (
            ticket.flows.filter(flow_type__in=[FlowType.RESOURCE_APPLY.value, FlowType.RESOURCE_BATCH_APPLY.value])
            .exclude(status__in=[TicketFlowStatus.SUCCEEDED, TicketFlowStatus.SKIPPED])
            .order_by("order", "id")
            .last()
        )

        # 覆盖详情并重新初始化派生字段(改前快照在覆盖前捕获)
        before_details = copy.deepcopy(ticket.details)
        builder = cls._patch_details(ticket, details)
        resource_builder = builder.resource_apply_builder or builder.resource_batch_apply_builder
        if not resource_builder:
            raise TicketModifyForbiddenException()

        cls._record(
            ticket,
            resource_flow,
            TicketModifyType.ADJUST_APPLY,
            operator,
            remark,
            ticket.details,
            change_count,
            before_details,
        )

        # 用重新初始化后的详情重建资源申请参数(覆盖 resource flow 的 details)
        resource_flow.details = resource_builder(ticket).get_params()
        resource_flow.save(update_fields=["details", "update_at"])

        # 重新申领资源
        from backend.ticket.flow_manager.manager import TicketFlowManager

        TicketFlowManager.get_ticket_flow_cls(resource_flow.flow_type)(resource_flow).retry()

        # 调整申请：仅当操作人非提单人时通知提单人
        if operator != ticket.creator:
            cls._notify_creator(ticket, operator, TicketModifyType.ADJUST_APPLY, remark)

    # ------------------------------------------------------------------
    # 权限校验
    # ------------------------------------------------------------------
    @staticmethod
    def _check_approve_permission(ticket, operator):
        """代为修改权限：与审批通过/拒绝同源，取业务下对应 DB 类型的管理员"""
        db_type = BuilderFactory.get_builder_cls(ticket.ticket_type).group
        admins = DBAdministrator.get_biz_db_type_admins(ticket.bk_biz_id, db_type)
        if operator not in admins:
            raise TicketModifyForbiddenException()

    @staticmethod
    def _check_replenish_permission(ticket, operator):
        """调整申请权限：与补货重试同权，取补货待办的处理人+协助人"""
        todo = ticket.todo_of_ticket.filter(type=TodoType.RESOURCE_REPLENISH, status=TodoStatus.TODO).first()
        if not todo or operator not in todo.operators + todo.helpers:
            raise TicketModifyForbiddenException()

    # ------------------------------------------------------------------
    # flow 操作 helper
    # ------------------------------------------------------------------
    @staticmethod
    def _get_running_itsm_flow(ticket):
        return (
            ticket.flows.filter(flow_type=FlowType.BK_ITSM.value, status=TicketFlowStatus.RUNNING)
            .order_by("order", "id")
            .last()
        )

    @staticmethod
    def _update_itsm_ticket(ticket, itsm_flow, operator):
        """按改后内容更新外部 ITSM V4 单据（待审批阶段每次重新编辑都会调用，幂等）"""
        from backend.components import ItsmV4Api
        from backend.components.itsm.backends.v4 import ItsmV4Backend

        builder = BuilderFactory.create_builder(ticket)
        old_params = builder.itsm_flow_builder(ticket).get_params()
        ticket_id = ItsmV4Backend.get_ticket_id(itsm_flow.flow_obj_id)
        update_params = ItsmV4Api.format_update_ticket_params(ticket_id, old_params, operator)
        ItsmV4Api.handle_ticket(update_params)

    @staticmethod
    def _freeze_flow(flow, exit_word, remark=""):
        """冻结节点：置 SKIPPED + 出口词/说明，不删除"""
        flow.status = TicketFlowStatus.SKIPPED
        flow.context.update({FlowContext.EXIT_WORD.value: exit_word})
        if remark:
            flow.context[FlowContext.MODIFY_SUMMARY.value] = remark
        flow.save(update_fields=["status", "context", "update_at"])

    @staticmethod
    def _close_flow_todos(flow, operator):
        """关闭节点关联的待办"""
        for todo in flow.todo_of_flow.filter(status=TodoStatus.TODO):
            todo.set_status(operator, TodoStatus.DONE_SUCCESS)

    @staticmethod
    def _patch_details(ticket, details):
        """覆盖单据详情并走 builder.patch_ticket_detail 重新初始化派生字段。

        建单时 create_ticket 依次执行 patch_ticket_detail -> init_ticket_flows，
        改单同样要先 patch 再重建 flow：新 details 中依赖后端派生的字段(集群/规格/实例/主机信息、
        db_version/charset 等)在此刷新，后续 build_tail_flows/get_params 才能读到完整内容。
        返回补丁后的 builder，供调用方据此重建尾巴 flow。
        """
        ticket.details = details
        ticket.save(update_fields=["details", "update_at"])
        builder = BuilderFactory.create_builder(ticket)
        builder.patch_ticket_detail()
        return builder

    @classmethod
    def _record(cls, ticket, flow, mode, operator, remark, new_details, change_count, before_details=None):
        """落库改单快照，diff 由前端计算后随 change_count 传入，后端只做快照。

        before_details 需由调用方在覆盖 details 之前显式传入；覆盖并 patch 后的 ticket.details 才是 after。
        """
        TicketModifyRecord.objects.create(
            ticket=ticket,
            flow=flow,
            mode=mode.value,
            operator=operator,
            remark=remark,
            before_details=before_details if before_details is not None else ticket.details,
            after_details=new_details,
            change_count=change_count,
            creator=operator,
            updater=operator,
        )

    @staticmethod
    def _shift_orders(ticket, from_order):
        """将 order >= from_order 的 flow 整体后移，为插入新节点腾位置"""
        ticket.flows.filter(order__gte=from_order).update(order=F("order") + 1)

    @classmethod
    def _insert_confirm_modify_and_rebuild_tail(cls, ticket, itsm_flow, auto_confirm=False, builder=None):
        """代为修改后：在审批节点之后插入确认修改节点，并覆盖 pending 尾巴"""
        builder = builder or BuilderFactory.create_builder(ticket)
        insert_order = itsm_flow.order + 1

        cls._shift_orders(ticket, insert_order)
        Flow.objects.create(
            ticket=ticket,
            flow_type=FlowType.CONFIRM_MODIFY.value,
            flow_alias=_("确认修改"),
            details={"operators": [ticket.creator], "auto_confirm": auto_confirm},
            order=insert_order,
        )
        cls._overwrite_pending_tail(ticket, builder, insert_order + 1)

    @staticmethod
    def _auto_confirm_modify(ticket):
        """代为修改时 operator==creator：直接结束确认修改节点(出口"确认无误")，不建待办/通知"""
        confirm_flow = (
            ticket.flows.filter(flow_type=FlowType.CONFIRM_MODIFY.value, status=TicketFlowStatus.PENDING)
            .order_by("order", "id")
            .last()
        )
        confirm_flow.context.update({FlowContext.EXIT_WORD.value: _("确认无误")})
        confirm_flow.status = TicketFlowStatus.SUCCEEDED
        confirm_flow.save(update_fields=["status", "context", "update_at"])

    @classmethod
    def _overwrite_pending_tail(cls, ticket, builder, start_order):
        """
        覆盖 pending 尾巴：不删除、不重建 id，按顺序用新详情覆盖
        结构收缩/扩张的边界情况仅会在改单改变流程结构时发生
        """
        old_pending = list(
            ticket.flows.filter(status=TicketFlowStatus.PENDING, order__gte=start_order).order_by("order", "id")
        )
        new_tail = builder.build_tail_flows()

        for idx, new_flow in enumerate(new_tail):
            order = start_order + idx
            if idx < len(old_pending):
                old_flow = old_pending[idx]
                old_flow.flow_type = new_flow.flow_type
                old_flow.flow_alias = new_flow.flow_alias
                old_flow.details = new_flow.details
                old_flow.retry_type = new_flow.retry_type
                old_flow.context = {}
                old_flow.flow_obj_id = ""
                old_flow.err_code = None
                old_flow.err_msg = None
                old_flow.status = TicketFlowStatus.PENDING
                old_flow.order = order
                old_flow.save(
                    update_fields=[
                        "flow_type",
                        "flow_alias",
                        "details",
                        "retry_type",
                        "context",
                        "flow_obj_id",
                        "err_code",
                        "err_msg",
                        "status",
                        "order",
                        "update_at",
                    ]
                )
            else:
                new_flow.order = order
                Flow.objects.bulk_create([new_flow])

        # 结构收缩时删除多余 pending 节点
        for extra in old_pending[len(new_tail) :]:
            extra.delete()
