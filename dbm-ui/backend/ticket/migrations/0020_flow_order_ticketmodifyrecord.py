# Generated manually for 改单能力(ticket modify) scaffolding
# 内容版本号不新增 Ticket 字段，由 TicketModifyRecord 记录数派生(modify.py: TicketModifyHandler._current_version)

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F

import backend.ticket.constants


def forwards(apps, schema_editor):
    # 存量 flow 的 order 以 id 回填，保证 (order, id) 顺序与历史 id 顺序一致，
    # 同时使 order 非零，便于后续改单时在中间插入/覆盖节点。
    Flow = apps.get_model("ticket", "Flow")
    Flow.objects.update(order=F("id"))


def backwards(apps, schema_editor):
    Flow = apps.get_model("ticket", "Flow")
    Flow.objects.update(order=0)


class Migration(migrations.Migration):

    dependencies = [
        ("ticket", "0019_migrate_ticketflowsconfig_cluster_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="flow",
            name="order",
            field=models.IntegerField(default=0, verbose_name="流程顺序"),
        ),
        migrations.CreateModel(
            name="TicketModifyRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("creator", models.CharField(max_length=64, verbose_name="创建人")),
                ("create_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updater", models.CharField(max_length=64, verbose_name="修改人")),
                ("update_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "mode",
                    models.CharField(
                        choices=backend.ticket.constants.TicketModifyType.get_choices(),
                        max_length=32,
                        verbose_name="改单模式",
                    ),
                ),
                ("operator", models.CharField(default="", max_length=64, verbose_name="操作人")),
                ("remark", models.CharField(default="", max_length=512, verbose_name="说明")),
                ("before_details", models.JSONField(default=dict, verbose_name="改前快照")),
                ("after_details", models.JSONField(default=dict, verbose_name="改后快照")),
                ("change_count", models.IntegerField(default=0, verbose_name="差异字段数")),
                (
                    "flow",
                    models.ForeignKey(
                        blank=True,
                        help_text="关联流程节点",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="modify_records",
                        to="ticket.flow",
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        help_text="关联工单",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="modify_records",
                        to="ticket.ticket",
                    ),
                ),
            ],
            options={
                "verbose_name": "单据改单记录(TicketModifyRecord)",
                "verbose_name_plural": "单据改单记录(TicketModifyRecord)",
            },
        ),
        migrations.AddIndex(
            model_name="ticketmodifyrecord",
            index=models.Index(fields=["ticket"], name="ticket_modi_ticket_36e0b6_idx"),
        ),
        migrations.AddIndex(
            model_name="ticketmodifyrecord",
            index=models.Index(fields=["flow"], name="ticket_modi_flow_id_f2c1a9_idx"),
        ),
        migrations.RunPython(forwards, backwards),
    ]
