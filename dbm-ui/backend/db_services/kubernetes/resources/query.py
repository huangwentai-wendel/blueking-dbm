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
from collections import defaultdict
from typing import Any, Dict, List

from django.db.models import Prefetch, QuerySet
from django.forms import model_to_dict
from django.utils.translation import ugettext_lazy as _

from backend.db_meta.enums import ClusterEntryType, ClusterType
from backend.db_meta.models import AppCache, Cluster, ClusterEntry, DBModule, Spec
from backend.db_meta.models.city_map import BKSubzone
from backend.db_services.dbbase.resources import query
from backend.db_services.dbbase.resources.query import CommonExportQueryResourceMixin, ResourceList
from backend.db_services.ipchooser.query.resource import ResourceQueryHelper
from backend.ticket.models import ClusterOperateRecord
from backend.utils.time import datetime2str


class KubernetesBaseExportQueryResourceMixin(CommonExportQueryResourceMixin):
    """补充k8s集群列表导出所需的header及数据父类"""

    @classmethod
    def update_headers(cls, headers, **kwargs):
        """
        更新的headers列表数据
        """
        # 大数据不需要从域名/模块字段值
        filtered_headers = list(filter(lambda header: header["id"] not in ["slave_domain", "db_module_name"], headers))
        return filtered_headers, kwargs["extra_headers"]

    @classmethod
    def update_cluster_info(cls, cluster, cluster_info, **kwargs):
        """
        更新的集群列表数据
        """
        # 删除cluster_info中的从域名/模块字段值
        del cluster_info["slave_domain"], cluster_info["db_module_name"]
        return cluster_info


class KubernetesBaseListRetrieveResource(query.BaseListRetrieveResource, KubernetesBaseExportQueryResourceMixin):
    """
    k8s相关组件资详情基类
    """

    cluster_types = []
    instance_roles = []
    fields = [
        {"name": _("集群名"), "key": "cluster_name"},
        {"name": _("集群别名"), "key": "cluster_alias"},
        {"name": _("集群类型"), "key": "cluster_type"},
        {"name": _("集群类型名"), "key": "cluster_type_name"},
        {"name": _("域名"), "key": "domain"},
        {"name": _("版本"), "key": "major_version"},
        {"name": _("创建人"), "key": "creator"},
        {"name": _("创建人"), "key": "creator"},
        {"name": _("创建时间"), "key": "create_at"},
        {"name": _("更新人"), "key": "updater"},
        {"name": _("更新时间"), "key": "update_at"},
    ]

    @classmethod
    def _filter_cluster_hook(
        cls,
        bk_biz_id,
        cluster_queryset: QuerySet,
        proxy_queryset: None,
        storage_queryset: None,
        limit: int,
        offset: int,
        **kwargs,
    ) -> ResourceList:

        count = cluster_queryset.count()
        limit = count if limit == -1 else limit
        if count == 0:
            return ResourceList(count=0, data=[])

        # 预取proxy_queryset，storage_queryset，clusterentry_set,加块查询效率
        cluster_list = cluster_queryset[offset : limit + offset].prefetch_related(
            Prefetch(
                "clusterentry_set", queryset=ClusterEntry.objects.select_related("forward_to"), to_attr="entries"
            ),
            "tags",
        )
        # 由于对 queryset 切片工作方式的模糊性，这里的values可能会获得非预期的排序，所以不要在切片后用values
        # cluster_ids = list(cluster_queryset.values_list("id", flat=True))
        cluster_ids = [c.id for c in cluster_list]

        # 获取集群与访问入口的映射
        # cluster_entry_map = ClusterEntry.get_cluster_entry_map(cluster_ids)
        cluster_entry_map = defaultdict(dict)

        # 获取DB模块的映射信息
        db_module_queryset = DBModule.objects.filter(cluster_type__in=cls.cluster_types)
        if bk_biz_id is not None:
            db_module_queryset = db_module_queryset.filter(bk_biz_id=bk_biz_id)
        # 提取所需的字段和构建映射
        db_module_names_map = {
            module["db_module_id"]: module["db_module_name"]
            for module in db_module_queryset.values("db_module_id", "db_module_name")
        }

        # 获取集群操作记录的映射关系
        cluster_operate_records_map = ClusterOperateRecord.get_cluster_records_map(cluster_ids)

        # 获取云区域信息和业务信息
        cloud_info = ResourceQueryHelper.search_cc_cloud(get_cache=True)
        try:
            biz_info = AppCache.objects.get(bk_biz_id=bk_biz_id)
        except AppCache.DoesNotExist:
            biz_info = None

        # 获取集群统计信息，只需要获取一次
        cluster_stats_map = Cluster.get_cluster_stats(bk_biz_id, cls.cluster_types)

        # 预取集群的规格信息
        db_types = set([ClusterType.cluster_type_to_db_type(cluster_type) for cluster_type in cls.cluster_types])
        kwargs["remote_spec_map"] = {s.spec_id: s for s in Spec.objects.filter(spec_cluster_type__in=db_types)}

        # 预取园区信息
        cluster_zone_map = BKSubzone.get_subzone_map(get_cache=True)

        # 将集群的查询结果序列化为集群字典信息
        clusters: List[Dict[str, Any]] = []
        for cluster in cluster_list:
            cluster_entry = []
            dns_to_clb = False
            for entry in cluster.entries:
                # 处理条目数据收集
                cluster_entry.append(
                    {"cluster_entry_type": entry.cluster_entry_type, "entry": entry.entry, "role": entry.role}
                )

                # 并行进行DNS->CLB检查
                if (
                    not dns_to_clb
                    and entry.cluster_entry_type == ClusterEntryType.DNS.value
                    and entry.entry == cluster.immute_domain
                    and entry.forward_to is not None
                    and entry.forward_to.cluster_entry_type == ClusterEntryType.CLB.value
                ):
                    dns_to_clb = True

            cluster_info = cls._to_cluster_representation(
                cluster=cluster,
                cluster_entry=cluster_entry,
                db_module_names_map=db_module_names_map,
                cluster_entry_map=cluster_entry_map,
                cluster_operate_records_map=cluster_operate_records_map,
                cloud_info=cloud_info,
                biz_info=biz_info,
                cluster_stats_map=cluster_stats_map,
                dns_to_clb=dns_to_clb,
                cluster_zone_map=cluster_zone_map,
                **kwargs,
            )
            clusters.append(cluster_info)

        return ResourceList(count=count, data=clusters)

    @classmethod
    def _to_cluster_representation(
        cls,
        cluster: Cluster,
        cluster_entry: List[Dict[str, str]],
        db_module_names_map: Dict[int, str],
        cluster_entry_map: Dict[int, Dict[str, str]],
        cluster_operate_records_map: Dict[int, List],
        cloud_info: Dict[str, Any],
        biz_info: AppCache,
        cluster_stats_map: Dict[str, Dict[str, int]],
        cluster_zone_map: Dict[str, str],
        dns_to_clb: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        将集群对象转为可序列化的 dict 结构
        @param cluster: model Cluster 对象, 增加了 storages 和 proxies 属性
        @param cluster_entry: 集群的访问入口列表
        @param db_module_names_map: key 是 db_module_id, value 是 db_module_name
        @param cluster_entry_map: key 是 cluster.id, value 是当前集群对应的 entry 映射
        @param cluster_operate_records_map: key 是 cluster.id, value 是当前集群对应的 操作记录 映射
        @param cloud_info: 云区域信息
        @param biz_info: 业务信息
        @param cluster_stats_map: 集群容量信息映射
        @param cluster_zone_map: 集群园区信息映射
        @param dns_to_clb: 是否将域名转换为 clb
        """
        cluster_spec = None
        cluster_entry_map_value = ClusterEntry.get_entries_map(entries=cluster.entries).get(cluster.id, {})
        bk_cloud_name = cloud_info.get(str(cluster.bk_cloud_id), {}).get("bk_cloud_name", "")
        cluster_zone_list = cluster.zone_list or []

        # 补充集群规格信息
        if cls.storage_spec_role:
            storage = next((inst for inst in cluster.storages if inst.instance_role == cls.storage_spec_role), None)
            cluster_spec_id = storage.machine.spec_id if storage else 0
            cluster_spec = kwargs["remote_spec_map"].get(cluster_spec_id)

        return {
            "id": cluster.id,
            "db_type": ClusterType.cluster_type_to_db_type(cluster.cluster_type),
            "phase": cluster.phase,
            "phase_name": cluster.get_phase_display(),
            "status": cluster.status,
            "operations": cluster_operate_records_map.get(cluster.id, []),
            "dns_to_clb": dns_to_clb,
            "cluster_time_zone": cluster.time_zone,
            "cluster_name": cluster.name,
            "cluster_alias": cluster.alias,
            "cluster_access_port": cluster.access_port,
            "cluster_stats": cluster_stats_map.get(cluster.immute_domain, {}),
            "cluster_type": cluster.cluster_type,
            "cluster_type_name": ClusterType.get_choice_label(cluster.cluster_type),
            "cluster_subzones": [cluster_zone_map.get(str(zone), "") for zone in cluster_zone_list],
            "cluster_subzone_ids": cluster_zone_list,
            "disaster_tolerance_level": cluster.disaster_tolerance_level,
            "master_domain": cluster_entry_map_value.get("master_domain", ""),
            "slave_domain": cluster_entry_map_value.get("slave_domain", ""),
            "cluster_entry": cluster_entry,
            "bk_biz_id": cluster.bk_biz_id,
            "bk_biz_name": "" if biz_info is None else biz_info.bk_biz_name,
            "bk_cloud_id": cluster.bk_cloud_id,
            "bk_cloud_name": bk_cloud_name,
            "major_version": cluster.major_version,
            "region": cluster.region,
            "city": cluster.region,
            "db_module_name": db_module_names_map.get(cluster.db_module_id, ""),
            "db_module_id": cluster.db_module_id,
            "creator": cluster.creator,
            "updater": cluster.updater,
            "create_at": datetime2str(cluster.create_at),
            "update_at": datetime2str(cluster.update_at),
            "cluster_spec": model_to_dict(cluster_spec) if cluster_spec else None,
            "tags": [tag.desc for tag in cluster.tags.all()],
            "zone_list": cluster.zone_list,
        }
