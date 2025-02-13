#!/usr/bin/env python3
# -*- coding:utf-8 _*-
"""
@author:fjg
@license: Apache Licence
@file: check.py
@time: 2021/04/09
@contact: fujiangong.fujg@bytedance.com
@site:
@software: PyCharm
"""
import time
from collections import defaultdict
import datetime
import ipaddress
from pathlib import Path
import re

import jsonpath
import requests
from kubernetes import client
from kubernetes.stream import stream

from clusters import K8sClusters, Cluster
from utils import RemoteClientCompass, config_obj, parse_resource, ONE_GIBI
from log import logger
from nodecollect import nodecheck, AllRun


class CheckGlobal(K8sClusters):
    def __init__(self):
        super(CheckGlobal, self).__init__()
        self.k8s_conf_list = self.get_clusters_conf()
        self.ssh_key_file = self.get_ssh_config()
        self.machines = self.get_machines()
        self.checkout = defaultdict(dict)

    def check_node_status(self):
        for cluster in self.clusters.keys():
            logger.info(f"start check cluster({cluster}) nodes status")
            node_list = jsonpath.jsonpath(self.clusters[cluster], '$.status[masters,nodes][*]')
            not_ready_list = list()
            ready_list = list()
            for node in node_list:
                if node['status'] != "Ready":
                    not_ready_list.append(node['name'])
                else:
                    ready_list.append(node['name'])
            status = True if len(not_ready_list) == 0 else False

            self.checkout[cluster]['node_status'] = {
                "data": {"ready": {'data': len(ready_list), 'name': ready_list},
                         'not_ready': {'data': len(not_ready_list), 'name': not_ready_list}}, 'status': status}

    def check_license(self):
        logger.info("start check license")
        license_info = self.get_license()
        not_after = license_info['spec']['notAfter']
        quota: dict = license_info['spec']['quota']
        used = license_info['status']['used']
        utc_format = '%Y-%m-%dT%H:%M:%S.%fZ'
        remain_days = (datetime.datetime.strptime(not_after, utc_format) - datetime.datetime.now()).days
        remain_physical_cpu = int(quota['physicalCpu']) - int(used['physicalCpu'])
        status = True if remain_days > 30 and remain_physical_cpu > int(quota['physicalCpu']) * 0.2 else False
        self.checkout['license'] = {
            'data': {'remain_days': remain_days, 'remain_physical_cpu': remain_physical_cpu}, 'status': status}
        if 'logicalCpu' in quota.keys():
            remain_logical_cpu = int(quota['logicalCpu']) - int(used['logicalCpu'])
            status = True if remain_logical_cpu > int(quota['logicalCpu']) * 0.2 else False
            self.checkout['license']['status'] = status
            self.checkout['license']['data']['remain_logical_cpu'] = remain_logical_cpu

    @staticmethod
    def get_response(url):
        ret = requests.get(url, verify=False)
        if ret.status_code not in [200, 202] and ret.content.decode() != 'ok':
            return False, ret.content.decode()
        return True, ret.content.decode()

    def check_component_status(self):
        logger.info("start check component status")
        for cluster in self.clusters.keys():
            self.checkout[cluster]['apiserver_status'] = list()
            self.checkout[cluster]['controller_status'] = list()
            self.checkout[cluster]['scheduler_status'] = list()
            for master_ip in self.clusters[cluster]['spec']['masters']:
                apiserver_url = f"https://{master_ip}:6443/healthz"
                logger.info(f'check component apiserver {master_ip}')
                status, content = self.get_response(apiserver_url)
                self.checkout[cluster]['apiserver_status'].append({master_ip: {'data': content, 'status': status}})
                controller_url = f"https://{master_ip}:10257/healthz"
                logger.info(f'check component controller {master_ip}')
                status, content = self.get_response(controller_url)
                self.checkout[cluster]['controller_status'].append(
                    {master_ip: {'data': content, 'status': status}})
                scheduler_url = f"https://{master_ip}:10259/healthz"
                logger.info(f'check scheduler apiserver {master_ip}')
                status, content = self.get_response(scheduler_url)
                self.checkout[cluster]['scheduler_status'].append({master_ip: {'data': content, 'status': status}})

    def check_etcd_status(self):
        logger.info("start check etcd status")
        for cluster in self.clusters.keys():
            self.checkout[cluster]['etcd_status'] = dict()
            for master_ip in self.clusters[cluster]['spec']['masters']:
                logger.info(f"check etcd {master_ip}")
                ssh_obj = RemoteClientCompass(master_ip, self.machines[master_ip]['spec']['auth']['user'],
                                              int(self.machines[master_ip]['spec']['sshPort']),
                                              self.machines[master_ip]['spec']['auth']['password'],
                                              self.machines[master_ip]['spec']['auth']['key'])
                for port in ['2379', '2381']:
                    get_member_cmd = f'ETCDCTL_API=3 /usr/local/etcd/bin/etcdctl --cacert=/var/lib/etcd/ssl/ca.crt --cert=/var/lib/etcd/ssl/etcd.crt --key=/var/lib/etcd/ssl/etcd.key --endpoints=https://{master_ip}:{port} endpoint health'
                    ret = ssh_obj.cmd(get_member_cmd)
                    status = ret[0].split()[2].rstrip(":")
                    if status == "healthy":
                        took_time = ret[0].split()[8]
                        self.checkout[cluster]['etcd_status'][f"{master_ip}:{port}"] = {'data': took_time,
                                                                                        'status': True}
                    else:
                        self.checkout[cluster]['etcd_status'][f"{master_ip}:{port}"] = {'data': ret, 'status': False}
                        break
                ssh_obj.close()

    def check_volumes_status(self):
        logger.info("start compass gluster volumes status")
        for master_ip in self.clusters['compass-stack']['spec']['masters']:
            ssh_obj = RemoteClientCompass(master_ip, self.machines[master_ip]['spec']['auth']['user'],
                                          int(self.machines[master_ip]['spec']['sshPort']),
                                          self.machines[master_ip]['spec']['auth']['password'],
                                          self.machines[master_ip]['spec']['auth']['key'])
            volumes_list_cmd = r"gluster volume list"
            volumes_list = ssh_obj.cmd(volumes_list_cmd)
            if volumes_list:
                self.checkout['volumes_status']['compass-stack'] = dict()
                for volume in volumes_list:
                    volume = volume.rstrip("\n")
                    logger.info(f"check compass gluster volumes {volume} brick")
                    self.checkout['volumes_status']['compass-stack'][volume] = {'data': list(), 'status': True}
                    volume_status_info_cmd = f"gluster volume status {volume} detail"
                    info = ssh_obj.cmd(volume_status_info_cmd)
                    brick_name = ""
                    for line in info:
                        if line.startswith("Brick"):
                            brick_name = line.split()[-1]
                        if line.startswith("Online"):
                            online = line.split()[-1].strip()
                            if online != "Y":
                                self.checkout['volumes_status']['compass-stack'][volume]['data'].append(brick_name)
                                self.checkout['volumes_status']['compass-stack'][volume]['status'] = False
                ssh_obj.close()
                break
        logger.info("start cargo gluster volumes status")
        ssh_obj_cargo = RemoteClientCompass(config_obj.get('cargo', 'node_ip'), config_obj.get('cargo', 'ssh_user'),
                                            int(config_obj.get('cargo', 'ssh_port')),
                                            config_obj.get('cargo', 'ssh_pwd'), '')
        container_list = ssh_obj_cargo.cmd(r"docker ps --format '{{.Names}}'")
        if "gluster-container" in container_list:
            volumes_list_cmd = r"docker exec gluster-container gluster volume list"
            volumes_list = ssh_obj_cargo.cmd(volumes_list_cmd)
            if volumes_list:
                self.checkout['volumes_status']['cargo'] = dict()
                for volume in volumes_list:
                    volume = volume.rstrip("\n")
                    logger.info(f"check cargo gluster volumes {volume} brick")
                    self.checkout['volumes_status']['cargo'][volume] = {'data': list(), 'status': True}
                    volume_status_info_cmd = f"docker exec gluster-container gluster volume status {volume} detail"
                    info = ssh_obj_cargo.cmd(volume_status_info_cmd)
                    brick_name = ""
                    for line in info:
                        if line.startswith("Brick"):
                            brick_name = line.split()[-1]
                        if line.startswith("Online"):
                            online = line.split()[-1].strip()
                            if online != "Y":
                                self.checkout['volumes_status']['cargo'][volume]['data'].append(brick_name)
                                self.checkout['volumes_status']['cargo'][volume]['status'] = False
                ssh_obj_cargo.close()

    def load_busybox_image(self):
        logger.info(f"load and push busybox image")
        registry = self.get_cm('platform-info', 'default')['data']['cargo_registry']
        # user = config_obj.get('cargo', 'harbor_user')
        # pwd = config_obj.get('cargo', 'harbor_pwd')
        # load_images_to_cargo(user, pwd, registry, './busybox-1.28.0')
        _busybox_images = f'{registry}/library/busybox:1.28.0'
        return _busybox_images

    def check_node_info(self):
        for cluster in self.clusters.keys():
            self.checkout[cluster]['node_info'] = dict()
        nodes_list = list()
        for machine in self.machines.keys():
            # logger.info(f"check node {machine} info")
            n = []
            n.insert(0, machine)
            cluster = self.machines[machine]['spec']['cluster']
            if cluster:
                user = self.machines[machine]['spec']['auth']['user']
                ssh_port = int(self.machines[machine]['spec']['sshPort'])
                pwd = self.machines[machine]['spec']['auth']['password']
                key = self.machines[machine]['spec']['auth']['key']
                n.insert(1, user)
                n.insert(2, ssh_port)
                n.insert(3, pwd)
                n.insert(4, key)
                n.insert(5, cluster)
            nodes_list.append(n)
        a = AllRun(nodes_list)
        a.concurrent_run()
        r = a.get_result()
        for i in r:
            for k, v in i.items():
                self.checkout[k]['node_info'].update(v)

    # ssh_obj = nodecheck(machine, user, ssh_port, pwd, key)
    # self.checkout[cluster]['node_info'][machine] = ssh_obj.start_check()
    # ssh_obj.close()

    def start_check(self):
        self.check_node_status()
        self.check_license()
        self.check_etcd_status()
        self.check_component_status()
        self.check_volumes_status()
        self.check_node_info()


class CheckK8s(Cluster):
    def __init__(self, kube_conf, checkout):
        super(CheckK8s, self).__init__(kube_conf)
        self.cluster_name = Path(kube_conf).name
        self.checkout = checkout
        self.pod_list = self.get_pods()
        self.svc_list = self.get_svc()
        self.nodes = self.get_node()

    def check_cidr(self):
        logger.info(f"check {self.cluster_name} cidr")
        cluster_info = self.get_cm('cluster-info', 'kube-system')
        pod_cidr_ip_num = ipaddress.ip_network(cluster_info['data']['cidr'], strict=True).num_addresses
        svc_cidr_ip_num = ipaddress.ip_network(cluster_info['data']['serviceIPRange'], strict=True).num_addresses
        pod_ip_list = jsonpath.jsonpath(self.pod_list, '$.items[*].status.pod_ip')
        node_ip_list = jsonpath.jsonpath(self.nodes, '$.items[*].status.addresses[*].address')
        svc_ip_used = len(jsonpath.jsonpath(self.svc_list, '$.items[*].spec.cluster_ip'))
        pod_ip_used = len(set(pod_ip_list) - set(node_ip_list))
        pod_status = True if pod_ip_used < pod_cidr_ip_num * 0.8 else False
        svc_status = True if svc_ip_used < svc_cidr_ip_num * 0.8 else False
        self.checkout[self.cluster_name]['pod_cidr'] = {'data': {'used': pod_ip_used, 'quota': pod_cidr_ip_num},
                                                        'status': pod_status}
        self.checkout[self.cluster_name]['svc_cidr'] = {'data': {'used': svc_ip_used, 'quota': svc_cidr_ip_num},
                                                        'status': svc_status}

    def check_pod_status(self):
        logger.info(f"check {self.cluster_name} pods status")
        status_list = jsonpath.jsonpath(self.pod_list, '$.items[*].status.phase')
        pod_status = {x['metadata']['name']: {
            'restart': sum(jsonpath.jsonpath(x, '$.status.container_statuses[*].restart_count')),
            'phase': ''.join(jsonpath.jsonpath(x, '$.status.phase'))} for x in self.pod_list['items']}
        pod_checkout = {x: {'data': 0, 'status': True, 'name': []} for x in status_list}
        for pod in pod_status.keys():
            pod_checkout[pod_status[pod]['phase']]['data'] += 1
            if pod_status[pod]['phase'] not in ['Running', 'Succeeded']:
                pod_checkout[pod_status[pod]['phase']]['status'] = False
                pod_checkout[pod_status[pod]['phase']]['name'].append(pod)
        self.checkout[self.cluster_name]['pods_status'] = pod_checkout

    def check_coredns_status(self):
        logger.info(f"check {self.cluster_name} coredns")
        coredns_deploy = self.get_coredns()
        if coredns_deploy['status']['available_replicas'] == coredns_deploy['status']['ready_replicas'] == \
                coredns_deploy['status']['replicas']:
            status = True
        else:
            status = False
        self.checkout[self.cluster_name]['coredns_status'] = {
            'data': {'available': coredns_deploy['status']['available_replicas'],
                     'ready': coredns_deploy['status']['ready_replicas'],
                     'replicas': coredns_deploy['status']['replicas']}, 'status': status}

    @staticmethod
    def __get_resource_json(crt, cru, crun, crs, clt, clu, clun, cls, mrt, mru, mrun, mrs, mlt, mlu, mlun, mls):
        data = {
            "cpu.request": {"data": {"total": crt, "unused": crun, "used": cru}, "status": crs},
            "cpu.limit": {"data": {"total": clt, "unused": clun, "used": clu}, "status": cls},
            "mem.request": {"data": {"total": mrt, "unused": mrun, "used": mru}, "status": mrs},
            "mem.limit": {"data": {"total": mlt, "unused": mlun, "used": mlu}, "status": mls}
        }
        return data

    def check_clusters_quotas(self):
        logger.info(f"check {self.cluster_name} cluster quota")
        clusters = self.get_clusterquotas()
        physical_cpu_total = parse_resource(clusters['system']['status']['physical']['capacity']['cpu'])
        physical_cpu_unused = parse_resource(clusters['system']['status']['physical']['allocatable']['cpu'])
        physical_cpu_used = "{:.2f}".format(physical_cpu_total - physical_cpu_unused)
        physical_cpu_status = True if physical_cpu_unused > physical_cpu_total * 0.2 else False
        physical_cpu_unused = "{:.2f}".format(physical_cpu_unused)

        physical_mem_total = parse_resource(clusters['system']['status']['physical']['capacity']['memory'])
        physical_mem_unused = parse_resource(clusters['system']['status']['physical']['allocatable']['memory'])
        physical_mem_used = "{:.2f}Gi".format((physical_mem_total - physical_mem_unused) / ONE_GIBI)
        physical_mem_status = True if physical_mem_unused > physical_mem_total * 0.2 else False
        physical_mem_unused = "{:.2f}Gi".format(physical_mem_unused / ONE_GIBI)
        physical_mem_total = "{:.2f}Gi".format(physical_mem_total / ONE_GIBI)

        logical_cpu_request_total = parse_resource(clusters['system']['status']['logical']['total']['requests.cpu'])
        logical_cpu_request_used = parse_resource(clusters['system']['status']['logical']['allocated']['requests.cpu'])
        logical_cpu_request_unused = "{:.2f}".format(logical_cpu_request_total - logical_cpu_request_used)
        logical_cpu_request_status = True if logical_cpu_request_used < logical_cpu_request_total * 0.8 else False
        logical_cpu_request_used = "{:.2f}".format(logical_cpu_request_used)

        logical_cpu_limit_total = parse_resource(clusters['system']['status']['logical']['total']['limits.cpu'])
        logical_cpu_limit_used = parse_resource(clusters['system']['status']['logical']['allocated']['limits.cpu'])
        logical_cpu_limit_unused = "{:.2f}".format(logical_cpu_limit_total - logical_cpu_limit_used)
        logical_cpu_limit_status = True if logical_cpu_limit_used < logical_cpu_limit_total * 0.8 else False
        logical_cpu_limit_used = "{:.2f}".format(logical_cpu_limit_used)

        logical_mem_request_total = parse_resource(clusters['system']['status']['logical']['total']['requests.memory'])
        logical_mem_request_used = parse_resource(
            clusters['system']['status']['logical']['allocated']['requests.memory'])
        logical_mem_request_unused = "{:.2f}Gi".format(
            (logical_mem_request_total - logical_mem_request_used) / ONE_GIBI)
        logical_mem_request_status = True if logical_mem_request_used < logical_mem_request_total * 0.8 else False
        logical_mem_request_total = "{:.2f}Gi".format(logical_mem_request_total / ONE_GIBI)
        logical_mem_request_used = "{:.2f}Gi".format(logical_mem_request_used / ONE_GIBI)

        logical_mem_limit_total = parse_resource(clusters['system']['status']['logical']['total']['limits.memory'])
        logical_mem_limit_used = parse_resource(clusters['system']['status']['logical']['allocated']['limits.memory'])
        logical_mem_limit_unused = "{:.2f}Gi".format((logical_mem_limit_total - logical_mem_limit_used) / ONE_GIBI)
        logical_mem_limit_status = True if logical_mem_limit_used < logical_mem_limit_total * 0.8 else False
        logical_mem_limit_total = "{:.2f}Gi".format(logical_mem_limit_total / ONE_GIBI)
        logical_mem_limit_used = "{:.2f}Gi".format(logical_mem_limit_used / ONE_GIBI)

        self.checkout[self.cluster_name]['cluster_quota'] = {
            "physical": {
                "cpu": {"data": {"total": physical_cpu_total, "unused": physical_cpu_unused, "used": physical_cpu_used},
                        "status": physical_cpu_status},
                "mem": {"data": {"total": physical_mem_total, "unused": physical_mem_unused, "used": physical_mem_used},
                        "status": physical_mem_status}
            },
            "logical": self.__get_resource_json(logical_cpu_request_total, logical_cpu_request_used,
                                                logical_cpu_request_unused, logical_cpu_request_status,
                                                logical_cpu_limit_total, logical_cpu_limit_used,
                                                logical_cpu_limit_unused, logical_cpu_limit_status,
                                                logical_mem_request_total, logical_mem_request_used,
                                                logical_mem_request_unused, logical_mem_request_status,
                                                logical_mem_limit_total, logical_mem_limit_used,
                                                logical_mem_limit_unused, logical_mem_limit_status)
        }

    def __get_checkout_for_tenant_and_partitions(self, objs):

        for key in objs.keys():
            cpu_request_total = parse_resource(objs[key]['status']['hard']['requests.cpu'])
            cpu_request_used = parse_resource(objs[key]['status']['used']['requests.cpu'])
            cpu_request_unused = "{:.2f}".format(cpu_request_total - cpu_request_used)
            cpu_request_status = True if cpu_request_used < cpu_request_total * 0.8 else False
            cpu_request_used = "{:.2f}".format(cpu_request_used)

            mem_request_total = parse_resource(objs[key]['status']['hard']['requests.memory'])
            mem_request_used = parse_resource(objs[key]['status']['used']['requests.memory'])
            mem_request_unused = "{:.2f}".format((mem_request_total - mem_request_used) / ONE_GIBI)
            mem_request_status = True if mem_request_used < mem_request_total * 0.8 else False
            mem_request_total = "{:.2f}Gi".format(mem_request_total / ONE_GIBI)
            mem_request_used = "{:.2f}Gi".format(mem_request_used / ONE_GIBI)

            cpu_limit_total = parse_resource(objs[key]['status']['hard']['limits.cpu'])
            cpu_limit_used = parse_resource(objs[key]['status']['used']['limits.cpu'])
            cpu_limit_unused = "{:.2f}".format(cpu_limit_total - cpu_limit_used)
            cpu_limit_status = True if cpu_limit_used < cpu_limit_total * 0.8 else False
            cpu_limit_used = "{:.2f}".format(cpu_limit_used)

            mem_limit_total = parse_resource(objs[key]['status']['hard']['limits.memory'])
            mem_limit_used = parse_resource(objs[key]['status']['used']['limits.memory'])
            mem_limit_unused = "{:.2f}".format((mem_limit_total - mem_limit_used) / ONE_GIBI)
            mem_limit_status = True if mem_limit_used < mem_limit_total * 0.8 else False
            mem_limit_total = "{:.2f}Gi".format(mem_limit_total / ONE_GIBI)
            mem_limit_used = "{:.2f}Gi".format(mem_limit_used / ONE_GIBI)
            data = dict()
            data[key] = self.__get_resource_json(cpu_request_total, cpu_request_used, cpu_request_unused,
                                                 cpu_request_status, cpu_limit_total, cpu_limit_used, cpu_limit_unused,
                                                 cpu_limit_status, mem_request_total, mem_request_used,
                                                 mem_request_unused, mem_request_status, mem_limit_total,
                                                 mem_limit_used, mem_limit_unused, mem_limit_status)
            return data

    def check_tenants_quotas(self):
        logger.info(f"check {self.cluster_name} tenants quotas")
        tenants = self.get_tenants()
        del tenants['system-tenant']
        data = self.__get_checkout_for_tenant_and_partitions(tenants)
        self.checkout[self.cluster_name]['tenants_quota'] = data

    def check_partitions_quotas(self):
        logger.info(f"check {self.cluster_name} partitions quotas")
        partitions = self.get_partitions()
        ignore_list = ['default', 'kube-node-lease', 'kube-public', 'kube-system']
        for key in ignore_list:
            del partitions[key]
        data = self.__get_checkout_for_tenant_and_partitions(partitions)
        self.checkout[self.cluster_name]['partitions_quota'] = data

    def pod_exec(self, name, ns, cmd):
        resp = stream(self.core_v1_api.connect_get_namespaced_pod_exec, name, ns,
                      command=cmd, stderr=True, stdin=True, stdout=True, tty=False)
        return resp

    def check_dns(self):
        logger.info(f"check {self.cluster_name} dns nslookup")
        external_domain = config_obj.get('kubernetes', 'externalDomain').split()
        internal_domain = config_obj.get('kubernetes', 'internalDomain').split()
        external_domain.extend(internal_domain)
        name = 'check-pod'
        ns = 'default'
        self.checkout[self.cluster_name]['dns_nslookup'] = {'data': [], 'status': True}
        for domain in external_domain:
            cmd = ['nslookup', domain]
            self.pod_exec(name, ns, cmd)
            resp = self.pod_exec(name, ns, cmd)
            pattern = re.compile("can't resolve")
            result = pattern.findall(resp)
            if result:
                status = False
                self.checkout[self.cluster_name]['dns_nslookup']['data'].append(domain)
                self.checkout[self.cluster_name]['dns_nslookup']['status'] = status

    def __get_node_pod_ip(self):
        node_pod_ip = dict()
        for pod in self.pod_list['items']:
            node_ip = pod['status']['host_ip']
            pod_ip = pod['status']['pod_ip']
            if node_ip not in node_pod_ip.keys():
                node_pod_ip[node_ip] = list()
            node_pod_ip[node_ip].append(pod_ip)
        for node in node_pod_ip.keys():
            node_pod_ip[node] = set(node_pod_ip[node]) - set(node_pod_ip.keys())
        return node_pod_ip

    def check_network(self):
        logger.info(f"check {self.cluster_name} network：pod -> node; pod -> pod (diff node)")
        ip_dict = self.__get_node_pod_ip()
        name = 'check-pod'
        ns = 'default'
        self.checkout[self.cluster_name]['network'] = dict()
        self.checkout[self.cluster_name]['network']["pod_to_node"] = {'data': [], 'status': True}
        self.checkout[self.cluster_name]['network']["pod_to_pod"] = {'data': [], 'status': True}
        for node in ip_dict.keys():
            cmd = ['ping', '-c', '2', node]
            resp = self.pod_exec(name, ns, cmd)
            pattern = re.compile(", 0% packet loss")
            result = pattern.findall(resp)
            if not result:
                self.checkout[self.cluster_name]['network']["pod_to_node"]['data'].append(node)
                self.checkout[self.cluster_name]['network']["pod_to_node"]['status'] = False
            pod_ip = list(ip_dict[node])[0]
            cmd1 = ['ping', '-c', '2', pod_ip]
            resp1 = self.pod_exec(name, ns, cmd1)
            result1 = pattern.findall(resp1)
            if not result1:
                self.checkout[self.cluster_name]['network']["pod_to_pod"]['data'].append(pod_ip)
                self.checkout[self.cluster_name]['network']["pod_to_pod"]['status'] = False

    def create_check_pod(self, image):
        logger.info(f"{self.cluster_name} create check pod")
        while True:
            try:
                self.core_v1_api.read_namespaced_pod('check-pod', 'default')
            except client.exceptions.ApiException:
                check_pod = {'apiVersion': 'v1', 'kind': 'Pod',
                             'metadata': {'name': 'check-pod', 'labels': {'app': 'check-pod'}},
                             'spec': {'containers': [{'name': 'busybox', 'image': image,
                                                      'command': ['sh', '-c',
                                                                  'echo Hello Kubernetes! && sleep 3600']}]}}
                self.core_v1_api.create_namespaced_pod('default', body=check_pod)

            resp = self.core_v1_api.read_namespaced_pod('check-pod', 'default').to_dict()
            if resp['status']['phase'] != 'Running':
                time.sleep(5)
            else:
                return True

    def del_check_pod(self):
        try:
            self.core_v1_api.delete_namespaced_pod('check-pod', 'default')
            logger.info('delete check-pod in default ns')
        except client.exceptions.ApiException:
            logger.info('pod check-pod not in default')

    def start_check(self):
        self.check_cidr()
        self.check_pod_status()
        self.check_coredns_status()
        self.check_clusters_quotas()
        self.check_tenants_quotas()
        self.check_partitions_quotas()
        self.check_dns()
        self.check_network()
