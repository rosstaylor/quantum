# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012,  Nachi Ueno,  NTT MCL,  Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License,  Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing,  software
#    distributed under the License is distributed on an "AS IS" BASIS,  WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND,  either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import shlex
import socket

import netaddr
from oslo.config import cfg

from quantum.agent.common import config
from quantum.agent.dhcp_agent import DictModel
from quantum.agent.linux import ip_lib
from quantum.agent.linux import utils
from quantum.openstack.common import log as logging


LOG = logging.getLogger(__name__)

DEVICE_OWNER_PROBE = 'network:probe'


class QuantumDebugAgent():

    OPTS = [
        # Needed for drivers
        cfg.StrOpt('admin_user',
                   help=_("Admin user")),
        cfg.StrOpt('admin_password',
                   help=_("Admin password"),
                   secret=True),
        cfg.StrOpt('admin_tenant_name',
                   help=_("Admin tenant name")),
        cfg.StrOpt('auth_url',
                   help=_("Authentication URL")),
        cfg.StrOpt('auth_strategy', default='keystone',
                   help=_("The type of authentication to use")),
        cfg.StrOpt('auth_region',
                   help=_("Authentication region")),
        cfg.BoolOpt('use_namespaces', default=True,
                    help=_("Use Linux network namespaces")),
        cfg.StrOpt('interface_driver',
                   help=_("The driver used to manage the virtual "
                          "interface.")),
        cfg.StrOpt('external_network_bridge', default='br-ex',
                   help=_("Name of bridge used for external network "
                          "traffic.")),
    ]

    def __init__(self, conf, client, driver):
        self.conf = conf
        self.root_helper = config.get_root_helper(conf)
        self.client = client
        self.driver = driver

    def _get_namespace(self, port):
        return "qprobe-%s" % port.id

    def create_probe(self, network_id):
        network = self._get_network(network_id)
        bridge = None
        if network.external:
            bridge = self.conf.external_network_bridge

        port = self._create_port(network)
        port.network = network
        interface_name = self.driver.get_device_name(port)
        namespace = None
        if self.conf.use_namespaces:
            namespace = self._get_namespace(port)

        if ip_lib.device_exists(interface_name, self.root_helper, namespace):
            LOG.debug(_('Reusing existing device: %s.'), interface_name)
        else:
            self.driver.plug(network.id,
                             port.id,
                             interface_name,
                             port.mac_address,
                             bridge=bridge,
                             namespace=namespace)
        ip_cidrs = []
        for fixed_ip in port.fixed_ips:
            subnet = fixed_ip.subnet
            net = netaddr.IPNetwork(subnet.cidr)
            ip_cidr = '%s/%s' % (fixed_ip.ip_address, net.prefixlen)
            ip_cidrs.append(ip_cidr)
        self.driver.init_l3(interface_name, ip_cidrs, namespace=namespace)
        return port

    def _get_subnet(self, subnet_id):
        subnet_dict = self.client.show_subnet(subnet_id)['subnet']
        return DictModel(subnet_dict)

    def _get_network(self, network_id):
        network_dict = self.client.show_network(network_id)['network']
        network = DictModel(network_dict)
        network.external = network_dict.get('router:external')
        obj_subnet = [self._get_subnet(s_id) for s_id in network.subnets]
        network.subnets = obj_subnet
        return network

    def clear_probe(self):
        ports = self.client.list_ports(device_id=socket.gethostname(),
                                       device_owner=DEVICE_OWNER_PROBE)
        info = ports['ports']
        for port in info:
            self.delete_probe(port['id'])

    def delete_probe(self, port_id):
        port = DictModel(self.client.show_port(port_id)['port'])
        network = self._get_network(port.network_id)
        bridge = None
        if network.external:
            bridge = self.conf.external_network_bridge
        ip = ip_lib.IPWrapper(self.root_helper)
        namespace = self._get_namespace(port)
        if self.conf.use_namespaces and ip.netns.exists(namespace):
            self.driver.unplug(self.driver.get_device_name(port),
                               bridge=bridge,
                               namespace=namespace)
            try:
                ip.netns.delete(namespace)
            except:
                LOG.warn(_('Failed to delete namespace %s'), namespace)
        else:
            self.driver.unplug(self.driver.get_device_name(port),
                               bridge=bridge)
        self.client.delete_port(port.id)

    def list_probes(self):
        ports = self.client.list_ports(device_owner=DEVICE_OWNER_PROBE)
        info = ports['ports']
        for port in info:
            port['device_name'] = self.driver.get_device_name(DictModel(port))
        return info

    def exec_command(self, port_id, command=None):
        port = DictModel(self.client.show_port(port_id)['port'])
        ip = ip_lib.IPWrapper(self.root_helper)
        namespace = self._get_namespace(port)
        if self.conf.use_namespaces:
            if not command:
                return "sudo ip netns exec %s" % self._get_namespace(port)
            namespace = ip.ensure_namespace(namespace)
            return namespace.netns.execute(shlex.split(command))
        else:
            return utils.execute(shlex.split(command))

    def ensure_probe(self, network_id):
        ports = self.client.list_ports(network_id=network_id,
                                       device_id=socket.gethostname(),
                                       device_owner=DEVICE_OWNER_PROBE)
        info = ports.get('ports', [])
        if info:
            return DictModel(info[0])
        else:
            return self.create_probe(network_id)

    def ping_all(self, network_id=None, timeout=1):
        if network_id:
            ports = self.client.list_ports(network_id=network_id)['ports']
        else:
            ports = self.client.list_ports()['ports']
        result = ""
        for port in ports:
            probe = self.ensure_probe(port['network_id'])
            if port['device_owner'] == DEVICE_OWNER_PROBE:
                continue
            for fixed_ip in port['fixed_ips']:
                address = fixed_ip['ip_address']
                subnet = self._get_subnet(fixed_ip['subnet_id'])
                if subnet.ip_version == 4:
                    ping_command = 'ping'
                else:
                    ping_command = 'ping6'
                result += self.exec_command(probe.id,
                                            '%s -c 1 -w %s %s' % (ping_command,
                                                                  timeout,
                                                                  address))
        return result

    def _create_port(self, network):
        body = dict(port=dict(
            admin_state_up=True,
            network_id=network.id,
            device_id='%s' % socket.gethostname(),
            device_owner=DEVICE_OWNER_PROBE,
            tenant_id=network.tenant_id,
            fixed_ips=[dict(subnet_id=s.id) for s in network.subnets]))
        port_dict = self.client.create_port(body)['port']
        port = DictModel(port_dict)
        port.network = network
        for fixed_ip in port.fixed_ips:
            fixed_ip.subnet = self._get_subnet(fixed_ip.subnet_id)
        return port
