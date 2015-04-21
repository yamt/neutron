# Copyright (c) 2012 OpenStack Foundation.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import sys
import time

import mock
from oslo_config import cfg
from oslo_log import log
import oslo_messaging
import testtools

from neutron.agent.common import ovs_lib
from neutron.agent.common import utils
from neutron.agent.linux import async_process
from neutron.agent.linux import ip_lib
from neutron.common import constants as n_const
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2.drivers.l2pop import rpc as l2pop_rpc
from neutron.plugins.openvswitch.common import constants
from neutron.tests.unit.plugins.openvswitch.agent import ovs_test_base


NOTIFIER = 'neutron.plugins.ml2.rpc.AgentNotifierApi'
OVS_LINUX_KERN_VERS_WITHOUT_VXLAN = "3.12.0"

FAKE_MAC = '00:11:22:33:44:55'
FAKE_IP1 = '10.0.0.1'
FAKE_IP2 = '10.0.0.2'


class FakeVif(object):
    ofport = 99
    port_name = 'name'


class CreateAgentConfigMap(ovs_test_base.OVSAgentConfigTestBase):

    def test_create_agent_config_map_succeeds(self):
        self.assertTrue(self.mod_agent.create_agent_config_map(cfg.CONF))

    def test_create_agent_config_map_fails_for_invalid_tunnel_config(self):
        # An ip address is required for tunneling but there is no default,
        # verify this for both gre and vxlan tunnels.
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_no_local_ip(self):
        # An ip address is required for tunneling but there is no default
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_for_invalid_tunnel_type(self):
        cfg.CONF.set_override('tunnel_types', ['foobar'], group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_multiple_tunnel_types(self):
        cfg.CONF.set_override('local_ip', '10.10.10.10', group='OVS')
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE,
                              p_const.TYPE_VXLAN], group='AGENT')
        cfgmap = self.mod_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['tunnel_types'],
                         [p_const.TYPE_GRE, p_const.TYPE_VXLAN])

    def test_create_agent_config_map_enable_distributed_routing(self):
        self.addCleanup(cfg.CONF.reset)
        # Verify setting only enable_tunneling will default tunnel_type to GRE
        cfg.CONF.set_override('enable_distributed_routing', True,
                              group='AGENT')
        cfgmap = self.mod_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['enable_distributed_routing'], True)


class TestOvsNeutronAgent(object):

    def setUp(self):
        super(TestOvsNeutronAgent, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        cfg.CONF.set_default('quitting_rpc_timeout', 10, 'AGENT')
        cfg.CONF.set_default('prevent_arp_spoofing', False, 'AGENT')
        kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                       'setup_integration_br'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                       'setup_ancillary_bridges',
                       return_value=[]),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.common.ovs_lib.BaseOVS.get_bridges'),
            mock.patch('neutron.openstack.common.loopingcall.'
                       'FixedIntervalLoopingCall',
                       new=MockFixedIntervalLoopingCall)):
            self.agent = self.mod_agent.OVSNeutronAgent(self._bridge_classes(),
                                                        **kwargs)
            # set back to true because initial report state will succeed due
            # to mocked out RPC calls
            self.agent.use_call = True
            self.agent.tun_br = self.br_tun_cls(br_name='br-tun')
        self.agent.sg_agent = mock.Mock()

    def _mock_port_bound(self, ofport=None, new_local_vlan=None,
                         old_local_vlan=None):
        port = mock.Mock()
        port.ofport = ofport
        net_uuid = 'my-net-uuid'
        fixed_ips = [{'subnet_id': 'my-subnet-uuid',
                      'ip_address': '1.1.1.1'}]
        if old_local_vlan is not None:
            self.agent.local_vlan_map[net_uuid] = (
                self.mod_agent.LocalVLANMapping(
                    old_local_vlan, None, None, None))
        with mock.patch.object(self.agent, 'int_br', autospec=True) as int_br:
            int_br.db_get_val.return_value = old_local_vlan
            int_br.set_db_attribute.return_value = True
            self.agent.port_bound(port, net_uuid, 'local', None, None,
                                  fixed_ips, "compute:None", False)
        int_br.db_get_val.assert_called_once_with("Port", mock.ANY, "tag")
        if new_local_vlan != old_local_vlan:
            int_br.set_db_attribute.assert_called_once_with(
                "Port", mock.ANY, "tag", new_local_vlan)
            if ofport != -1:
                int_br.delete_flows.assert_called_once_with(
                    in_port=port.ofport)
            else:
                self.assertFalse(int_br.delete_flows.called)
        else:
            self.assertFalse(int_br.set_db_attribute.called)
            self.assertFalse(int_br.delete_flows.called)

    def test_check_agent_configurations_for_dvr_raises(self):
        self.agent.enable_distributed_routing = True
        self.agent.enable_tunneling = True
        self.agent.l2_pop = False
        self.assertRaises(ValueError,
                          self.agent._check_agent_configurations)

    def test_check_agent_configurations_for_dvr(self):
        self.agent.enable_distributed_routing = True
        self.agent.enable_tunneling = True
        self.agent.l2_pop = True
        self.assertIsNone(self.agent._check_agent_configurations())

    def test_check_agent_configurations_for_dvr_with_vlan(self):
        self.agent.enable_distributed_routing = True
        self.agent.enable_tunneling = False
        self.agent.l2_pop = False
        self.assertIsNone(self.agent._check_agent_configurations())

    def test_port_bound_deletes_flows_for_valid_ofport(self):
        self._mock_port_bound(ofport=1, new_local_vlan=1)

    def test_port_bound_ignores_flows_for_invalid_ofport(self):
        self._mock_port_bound(ofport=-1, new_local_vlan=1)

    def test_port_bound_does_not_rewire_if_already_bound(self):
        self._mock_port_bound(ofport=-1, new_local_vlan=1, old_local_vlan=1)

    def _test_port_dead(self, cur_tag=None):
        port = mock.Mock()
        port.ofport = 1
        with mock.patch.object(self.agent, 'int_br') as int_br:
            int_br.db_get_val.return_value = cur_tag
            self.agent.port_dead(port)
        if cur_tag == self.mod_agent.DEAD_VLAN_TAG:
            self.assertFalse(int_br.set_db_attribute.called)
            self.assertFalse(int_br.drop_port.called)
        else:
            int_br.assert_has_calls([
                mock.call.set_db_attribute("Port", mock.ANY, "tag",
                                           self.mod_agent.DEAD_VLAN_TAG),
                mock.call.drop_port(in_port=port.ofport),
            ])

    def test_port_dead(self):
        self._test_port_dead()

    def test_port_dead_with_port_already_dead(self):
        self._test_port_dead(self.mod_agent.DEAD_VLAN_TAG)

    def mock_scan_ports(self, vif_port_set=None, registered_ports=None,
                        updated_ports=None, port_tags_dict=None):
        if port_tags_dict is None:  # Because empty dicts evaluate as False.
            port_tags_dict = {}
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                              return_value=vif_port_set),
            mock.patch.object(self.agent.int_br, 'get_port_tag_dict',
                              return_value=port_tags_dict)
        ):
            return self.agent.scan_ports(registered_ports, updated_ports)

    def test_scan_ports_returns_current_only_for_unchanged_ports(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 3])
        expected = {'current': vif_port_set}
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_returns_port_changes(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]), removed=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def _test_scan_ports_with_updated_ports(self, updated_ports):
        vif_port_set = set([1, 3, 4])
        registered_ports = set([1, 2, 4])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([4]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_finds_known_updated_ports(self):
        self._test_scan_ports_with_updated_ports(set([4]))

    def test_scan_ports_ignores_unknown_updated_ports(self):
        # the port '5' was not seen on current ports. Hence it has either
        # never been wired or already removed and should be ignored
        self._test_scan_ports_with_updated_ports(set([4, 5]))

    def test_scan_ports_ignores_updated_port_if_removed(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        updated_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([1]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_no_vif_changes_returns_updated_port_only(self):
        vif_port_set = set([1, 2, 3])
        registered_ports = set([1, 2, 3])
        updated_ports = set([2])
        expected = dict(current=vif_port_set, updated=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_update_ports_returns_changed_vlan(self):
        br = self.br_int_cls('br-int')
        mac = "ca:fe:de:ad:be:ef"
        port = ovs_lib.VifPort(1, 1, 1, mac, br)
        lvm = self.mod_agent.LocalVLANMapping(
            1, '1', None, 1, {port.vif_id: port})
        local_vlan_map = {'1': lvm}
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        port_tags_dict = {1: []}
        expected = dict(
            added=set([3]), current=vif_port_set,
            removed=set([2]), updated=set([1])
        )
        with contextlib.nested(
            mock.patch.dict(self.agent.local_vlan_map, local_vlan_map),
            mock.patch.object(self.agent, 'tun_br', autospec=True),
        ):
            actual = self.mock_scan_ports(
                vif_port_set, registered_ports, port_tags_dict=port_tags_dict)
        self.assertEqual(expected, actual)

    def test_treat_devices_added_returns_raises_for_missing_device(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              side_effect=Exception()),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.Mock())):
            self.assertRaises(
                self.mod_agent.DeviceListRetrievalError,
                self.agent.treat_devices_added_or_updated, [{}], False)

    def _mock_treat_devices_added_updated(self, details, port, func_name):
        """Mock treat devices added or updated.

        :param details: the details to return for the device
        :param port: the port that get_vif_port_by_id should return
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[details]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=port),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, func_name)
        ) as (get_dev_fn, get_vif_func, upd_dev_up, upd_dev_down, func):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should not raise
            self.assertFalse(skip_devs)
        return func.called

    def test_treat_devices_added_updated_ignores_invalid_ofport(self):
        port = mock.Mock()
        port.ofport = -1
        self.assertFalse(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port, 'port_dead'))

    def test_treat_devices_added_updated_marks_unknown_port_as_dead(self):
        port = mock.Mock()
        port.ofport = 1
        self.assertTrue(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port, 'port_dead'))

    def test_treat_devices_added_does_not_process_missing_port(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details'),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=None)
        ) as (get_dev_fn, get_vif_func):
            self.assertFalse(get_dev_fn.called)

    def test_treat_devices_added_updated_updates_known_port(self):
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        self.assertTrue(self._mock_treat_devices_added_updated(
            details, mock.Mock(), 'treat_vif_port'))

    def test_treat_devices_added_updated_skips_if_port_not_found(self):
        dev_mock = mock.MagicMock()
        dev_mock.__getitem__.return_value = 'the_skipped_one'
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[dev_mock]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=None),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, get_vif_func, upd_dev_up,
              upd_dev_down, treat_vif_port):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should return False for resync and no device
            # processed
            self.assertEqual(['the_skipped_one'], skip_devs)
            self.assertFalse(treat_vif_port.called)
            self.assertFalse(upd_dev_down.called)
            self.assertFalse(upd_dev_up.called)

    def test_treat_devices_added_updated_put_port_down(self):
        fake_details_dict = {'admin_state_up': False,
                             'port_id': 'xxx',
                             'device': 'xxx',
                             'network_id': 'yyy',
                             'physical_network': 'foo',
                             'segmentation_id': 'bar',
                             'network_type': 'baz',
                             'fixed_ips': [{'subnet_id': 'my-subnet-uuid',
                                            'ip_address': '1.1.1.1'}],
                             'device_owner': 'compute:None'
                             }

        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[fake_details_dict]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.MagicMock()),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, get_vif_func, upd_dev_up,
              upd_dev_down, treat_vif_port):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should return False for resync
            self.assertFalse(skip_devs)
            self.assertTrue(treat_vif_port.called)
            self.assertTrue(upd_dev_down.called)

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_removed([{}]))

    def _mock_treat_devices_removed(self, port_exists):
        details = dict(exists=port_exists)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent, 'port_unbound') as port_unbound:
                self.assertFalse(self.agent.treat_devices_removed([{}]))
        self.assertTrue(port_unbound.called)

    def test_treat_devices_removed_unbinds_port(self):
        self._mock_treat_devices_removed(True)

    def test_treat_devices_removed_ignores_missing_port(self):
        self._mock_treat_devices_removed(False)

    def _test_process_network_ports(self, port_info):
        with contextlib.nested(
            mock.patch.object(self.agent.sg_agent, "setup_port_filters"),
            mock.patch.object(self.agent, "treat_devices_added_or_updated",
                              return_value=[]),
            mock.patch.object(self.agent, "treat_devices_removed",
                              return_value=False)
        ) as (setup_port_filters, device_added_updated, device_removed):
            self.assertFalse(self.agent.process_network_ports(port_info,
                                                              False))
            setup_port_filters.assert_called_once_with(
                port_info['added'], port_info.get('updated', set()))
            device_added_updated.assert_called_once_with(
                port_info['added'] | port_info.get('updated', set()), False)
            device_removed.assert_called_once_with(port_info['removed'])

    def test_process_network_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0']),
             'removed': set(['eth0']),
             'added': set(['eth1'])})

    def test_process_network_port_with_updated_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0', 'tap1']),
             'updated': set(['tap1', 'eth1']),
             'removed': set(['eth0']),
             'added': set(['eth1'])})

    def test_report_state(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            self.agent.int_br_device_count = 5
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)
            self.assertNotIn("start_flag", self.agent.agent_state)
            self.assertFalse(self.agent.use_call)
            self.assertEqual(
                self.agent.agent_state["configurations"]["devices"],
                self.agent.int_br_device_count
            )
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, False)

    def test_report_state_fail(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            report_st.side_effect = Exception()
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)

    def test_network_delete(self):
        with contextlib.nested(
            mock.patch.object(self.agent, "reclaim_local_vlan"),
            mock.patch.object(self.agent.tun_br, "cleanup_tunnel_port")
        ) as (recl_fn, clean_tun_fn):
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(recl_fn.called)
            self.agent.local_vlan_map["123"] = "LVM object"
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(clean_tun_fn.called)
            recl_fn.assert_called_with("123")

    def test_port_update(self):
        port = {"id": "123",
                "network_id": "124",
                "admin_state_up": False}
        self.agent.port_update("unused_context",
                               port=port,
                               network_type="vlan",
                               segmentation_id="1",
                               physical_network="physnet")
        self.assertEqual(set(['123']), self.agent.updated_ports)

    def test_port_delete(self):
        port_id = "123"
        port_name = "foo"
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.MagicMock(
                                      port_name=port_name)),
            mock.patch.object(self.agent.int_br, "delete_port")
        ) as (get_vif_func, del_port_func):
            self.agent.port_delete("unused_context",
                                   port_id=port_id)
            self.assertTrue(get_vif_func.called)
            del_port_func.assert_called_once_with(port_name)

    def test_setup_physical_bridges(self):
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(utils, "execute"),
            mock.patch.object(self.agent, 'br_phys_cls'),
            mock.patch.object(self.agent, 'int_br'),
        ) as (devex_fn, sysexit_fn, utilsexec_fn,
              phys_br_cls, int_br):
            devex_fn.return_value = True
            parent = mock.MagicMock()
            phys_br = phys_br_cls()
            parent.attach_mock(phys_br_cls, 'phys_br_cls')
            parent.attach_mock(phys_br, 'phys_br')
            parent.attach_mock(int_br, 'int_br')
            phys_br.add_patch_port.return_value = "phy_ofport"
            int_br.add_patch_port.return_value = "int_ofport"
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            expected_calls = [
                mock.call.phys_br_cls('br-eth'),
                mock.call.phys_br.setup_controllers(mock.ANY),
                mock.call.phys_br.setup_default_table(),
                mock.call.int_br.delete_port('int-br-eth'),
                mock.call.phys_br.delete_port('phy-br-eth'),
                mock.call.int_br.add_patch_port('int-br-eth',
                                                constants.NONEXISTENT_PEER),
                mock.call.phys_br.add_patch_port('phy-br-eth',
                                                 constants.NONEXISTENT_PEER),
                mock.call.int_br.drop_port(in_port='int_ofport'),
                mock.call.phys_br.drop_port(in_port='phy_ofport'),
                mock.call.int_br.set_db_attribute('Interface', 'int-br-eth',
                                                  'options:peer',
                                                  'phy-br-eth'),
                mock.call.phys_br.set_db_attribute('Interface', 'phy-br-eth',
                                                   'options:peer',
                                                   'int-br-eth'),
            ]
            parent.assert_has_calls(expected_calls)
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "int_ofport")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "phy_ofport")

    def test_setup_physical_bridges_using_veth_interconnection(self):
        self.agent.use_veth_interconnection = True
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(utils, "execute"),
            mock.patch.object(self.agent, 'br_phys_cls'),
            mock.patch.object(self.agent, 'int_br'),
            mock.patch.object(ip_lib.IPWrapper, "add_veth"),
            mock.patch.object(ip_lib.IpLinkCommand, "delete"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_up"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_mtu"),
            mock.patch.object(ovs_lib.BaseOVS, "get_bridges")
        ) as (devex_fn, sysexit_fn, utilsexec_fn, phys_br_cls, int_br,
              addveth_fn, linkdel_fn, linkset_fn, linkmtu_fn, get_br_fn):
            devex_fn.return_value = True
            parent = mock.MagicMock()
            parent.attach_mock(utilsexec_fn, 'utils_execute')
            parent.attach_mock(linkdel_fn, 'link_delete')
            parent.attach_mock(addveth_fn, 'add_veth')
            addveth_fn.return_value = (ip_lib.IPDevice("int-br-eth1"),
                                       ip_lib.IPDevice("phy-br-eth1"))
            phys_br = phys_br_cls()
            phys_br.add_port.return_value = "phys_veth_ofport"
            int_br.add_port.return_value = "int_veth_ofport"
            get_br_fn.return_value = ["br-eth"]
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            expected_calls = [mock.call.link_delete(),
                              mock.call.utils_execute(['udevadm',
                                                       'settle',
                                                       '--timeout=10']),
                              mock.call.add_veth('int-br-eth',
                                                 'phy-br-eth')]
            parent.assert_has_calls(expected_calls, any_order=False)
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "int_veth_ofport")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "phys_veth_ofport")

    def test_get_peer_name(self):
            bridge1 = "A_REALLY_LONG_BRIDGE_NAME1"
            bridge2 = "A_REALLY_LONG_BRIDGE_NAME2"
            self.agent.use_veth_interconnection = True
            self.assertEqual(len(self.agent.get_peer_name('int-', bridge1)),
                             n_const.DEVICE_NAME_MAX_LEN)
            self.assertEqual(len(self.agent.get_peer_name('int-', bridge2)),
                             n_const.DEVICE_NAME_MAX_LEN)
            self.assertNotEqual(self.agent.get_peer_name('int-', bridge1),
                                self.agent.get_peer_name('int-', bridge2))

    def test_setup_tunnel_br(self):
        self.tun_br = mock.Mock()
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, "add_patch_port",
                              return_value=1),
            mock.patch.object(self.agent, 'tun_br', autospec=True),
            mock.patch.object(sys, "exit")
        ) as (intbr_patch_fn, tun_br, exit_fn):
            tun_br.add_patch_port.return_value = 2
            self.agent.reset_tunnel_br(None)
            self.agent.setup_tunnel_br()
            self.assertTrue(intbr_patch_fn.called)

    def test_setup_tunnel_port(self):
        self.agent.tun_br = mock.Mock()
        self.agent.l2_pop = False
        self.agent.udp_vxlan_port = 8472
        self.agent.tun_br_ofports['vxlan'] = {}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, "add_tunnel_port",
                              return_value='6'),
            mock.patch.object(self.agent.tun_br, "add_flow")
        ) as (add_tun_port_fn, add_flow_fn):
            self.agent._setup_tunnel_port(self.agent.tun_br, 'portname',
                                          '1.2.3.4', 'vxlan')
            self.assertTrue(add_tun_port_fn.called)

    def test_port_unbound(self):
        with mock.patch.object(self.agent, "reclaim_local_vlan") as reclvl_fn:
            self.agent.enable_tunneling = True
            lvm = mock.Mock()
            lvm.network_type = "gre"
            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.local_vlan_map["netuid12345"] = lvm
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertTrue(reclvl_fn.called)
            reclvl_fn.called = False

            lvm.vif_ports = {}
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.port_unbound("vif3", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

    def _prepare_l2_pop_ofports(self):
        lvm1 = mock.Mock()
        lvm1.network_type = 'gre'
        lvm1.vlan = 'vlan1'
        lvm1.segmentation_id = 'seg1'
        lvm1.tun_ofports = set(['1'])
        lvm2 = mock.Mock()
        lvm2.network_type = 'gre'
        lvm2.vlan = 'vlan2'
        lvm2.segmentation_id = 'seg2'
        lvm2.tun_ofports = set(['1', '2'])
        self.agent.local_vlan_map = {'net1': lvm1, 'net2': lvm2}
        self.agent.tun_br_ofports = {'gre':
                                     {'1.1.1.1': '1', '2.2.2.2': '2'}}
        self.agent.arp_responder_enabled = True

    def test_fdb_ignore_network(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net3': {}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_flow'),
            mock.patch.object(self.agent.tun_br, 'delete_flows'),
            mock.patch.object(self.agent, '_setup_tunnel_port'),
            mock.patch.object(self.agent, 'cleanup_tunnel_port')
        ) as (add_flow_fn, del_flow_fn, add_tun_fn, clean_tun_fn):
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_flow_fn.called)
            self.assertFalse(add_tun_fn.called)
            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(del_flow_fn.called)
            self.assertFalse(clean_tun_fn.called)

    def test_fdb_ignore_self(self):
        self._prepare_l2_pop_ofports()
        self.agent.local_ip = 'agent_ip'
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports':
                      {'agent_ip':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}
        with mock.patch.object(self.agent.tun_br,
                               "deferred") as defer_fn:
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(defer_fn.called)

            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(defer_fn.called)

    def test_fdb_add_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net1':
                     {'network_type': 'gre',
                      'segment_id': 'tun1',
                      'ports':
                      {'2.2.2.2':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}

        with contextlib.nested(
            mock.patch.object(self.agent, 'tun_br', autospec=True),
            mock.patch.object(self.agent, '_setup_tunnel_port', autospec=True),
        ) as (tun_br, add_tun_fn):
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            deferred_br_call = mock.call.deferred().__enter__()
            expected_calls = [
                deferred_br_call.install_arp_responder('vlan1', FAKE_IP1,
                                                       FAKE_MAC),
                deferred_br_call.install_unicast_to_tun('vlan1', 'seg1', '2',
                                                        FAKE_MAC),
                deferred_br_call.install_flood_to_tun('vlan1', 'seg1',
                                                      set(['1', '2'])),
            ]
            tun_br.assert_has_calls(expected_calls)

    def test_fdb_del_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports':
                      {'2.2.2.2':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}
        with mock.patch.object(self.agent, 'tun_br', autospec=True) as br_tun:
            self.agent.fdb_remove(None, fdb_entry)
            deferred_br_call = mock.call.deferred().__enter__()
            expected_calls = [
                mock.call.deferred(),
                mock.call.deferred().__enter__(),
                deferred_br_call.delete_arp_responder('vlan2', FAKE_IP1),
                deferred_br_call.delete_unicast_to_tun('vlan2', FAKE_MAC),
                deferred_br_call.install_flood_to_tun('vlan2', 'seg2',
                                                      set(['1'])),
                deferred_br_call.delete_port('gre-02020202'),
                deferred_br_call.cleanup_tunnel_port('2'),
                mock.call.deferred().__exit__(None, None, None),
            ]
            br_tun.assert_has_calls(expected_calls)

    def test_fdb_add_port(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net1':
                     {'network_type': 'gre',
                      'segment_id': 'tun1',
                      'ports': {'1.1.1.1': [l2pop_rpc.PortInfo(FAKE_MAC,
                                                               FAKE_IP1)]}}}
        with contextlib.nested(
            mock.patch.object(self.agent, 'tun_br', autospec=True),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (tun_br, add_tun_fn):
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            fdb_entry['net1']['ports']['10.10.10.10'] = [
                l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1)]
            self.agent.fdb_add(None, fdb_entry)
            deferred_br = tun_br.deferred().__enter__()
            add_tun_fn.assert_called_with(
                deferred_br, 'gre-0a0a0a0a', '10.10.10.10', 'gre')

    def test_fdb_del_port(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports': {'2.2.2.2': [n_const.FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'delete_port'),
        ) as (defer_fn, delete_port_fn):
            self.agent.fdb_remove(None, fdb_entry)
            deferred_br = defer_fn().__enter__()
            deferred_br.delete_port.assert_called_once_with('gre-02020202')
            self.assertFalse(delete_port_fn.called)

    def test_fdb_update_chg_ip(self):
        self._prepare_l2_pop_ofports()
        fdb_entries = {'chg_ip':
                       {'net1':
                        {'agent_ip':
                         {'before': [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1)],
                          'after': [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP2)]}}}}
        with mock.patch.object(self.agent.tun_br, 'deferred') as deferred_fn:
            self.agent.fdb_update(None, fdb_entries)
            deferred_br = deferred_fn().__enter__()
            deferred_br.assert_has_calls([
                mock.call.install_arp_responder('vlan1', FAKE_IP2, FAKE_MAC),
                mock.call.delete_arp_responder('vlan1', FAKE_IP1)
            ])

    def test_del_fdb_flow_idempotency(self):
        lvm = mock.Mock()
        lvm.network_type = 'gre'
        lvm.vlan = 'vlan1'
        lvm.segmentation_id = 'seg1'
        lvm.tun_ofports = set(['1', '2'])
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'mod_flow'),
            mock.patch.object(self.agent.tun_br, 'delete_flows')
        ) as (mod_flow_fn, delete_flows_fn):
            self.agent.del_fdb_flow(self.agent.tun_br, n_const.FLOODING_ENTRY,
                                    '1.1.1.1', lvm, '3')
            self.assertFalse(mod_flow_fn.called)
            self.assertFalse(delete_flows_fn.called)

    def test_recl_lv_port_to_preserve(self):
        self._prepare_l2_pop_ofports()
        self.agent.l2_pop = True
        self.agent.enable_tunneling = True
        with mock.patch.object(self.agent, 'tun_br', autospec=True) as tun_br:
            self.agent.reclaim_local_vlan('net1')
            self.assertFalse(tun_br.cleanup_tunnel_port.called)

    def test_recl_lv_port_to_remove(self):
        self._prepare_l2_pop_ofports()
        self.agent.l2_pop = True
        self.agent.enable_tunneling = True
        with mock.patch.object(self.agent, 'tun_br', autospec=True) as tun_br:
            self.agent.reclaim_local_vlan('net2')
            tun_br.delete_port.assert_called_once_with('gre-02020202')

    def test_daemon_loop_uses_polling_manager(self):
        with mock.patch(
            'neutron.agent.common.polling.get_polling_manager') as mock_get_pm:
            with mock.patch.object(self.agent, 'rpc_loop') as mock_loop:
                self.agent.daemon_loop()
        mock_get_pm.assert_called_with(True,
                                       constants.DEFAULT_OVSDBMON_RESPAWN)
        mock_loop.assert_called_once_with(polling_manager=mock.ANY)

    def test_setup_tunnel_port_invalid_ofport(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value=ovs_lib.INVALID_OFPORT),
            mock.patch.object(self.mod_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            ofport = self.agent._setup_tunnel_port(
                self.agent.tun_br, 'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port, self.agent.dont_fragment)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)

    def test_setup_tunnel_port_error_negative_df_disabled(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value=ovs_lib.INVALID_OFPORT),
            mock.patch.object(self.mod_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            self.agent.dont_fragment = False
            ofport = self.agent._setup_tunnel_port(
                self.agent.tun_br, 'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port, self.agent.dont_fragment)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)

    def test_tunnel_sync_with_ml2_plugin(self):
        fake_tunnel_details = {'tunnels': [{'ip_address': '100.101.31.15'}]}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'tunnel_sync',
                              return_value=fake_tunnel_details),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (tunnel_sync_rpc_fn, _setup_tunnel_port_fn):
            self.agent.tunnel_types = ['vxlan']
            self.agent.tunnel_sync()
            expected_calls = [mock.call(self.agent.tun_br, 'vxlan-64651f0f',
                                        '100.101.31.15', 'vxlan')]
            _setup_tunnel_port_fn.assert_has_calls(expected_calls)

    def test_tunnel_sync_invalid_ip_address(self):
        fake_tunnel_details = {'tunnels': [{'ip_address': '300.300.300.300'},
                                           {'ip_address': '100.100.100.100'}]}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'tunnel_sync',
                              return_value=fake_tunnel_details),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (tunnel_sync_rpc_fn, _setup_tunnel_port_fn):
            self.agent.tunnel_types = ['vxlan']
            self.agent.tunnel_sync()
            _setup_tunnel_port_fn.assert_called_once_with(self.agent.tun_br,
                                                          'vxlan-64646464',
                                                          '100.100.100.100',
                                                          'vxlan')

    def test_tunnel_update(self):
        kwargs = {'tunnel_ip': '10.10.10.10',
                  'tunnel_type': 'gre'}
        self.agent._setup_tunnel_port = mock.Mock()
        self.agent.enable_tunneling = True
        self.agent.tunnel_types = ['gre']
        self.agent.l2_pop = False
        self.agent.tunnel_update(context=None, **kwargs)
        expected_calls = [
            mock.call(self.agent.tun_br, 'gre-0a0a0a0a', '10.10.10.10', 'gre')]
        self.agent._setup_tunnel_port.assert_has_calls(expected_calls)

    def test_tunnel_delete(self):
        kwargs = {'tunnel_ip': '10.10.10.10',
                  'tunnel_type': 'gre'}
        self.agent.enable_tunneling = True
        self.agent.tunnel_types = ['gre']
        self.agent.tun_br_ofports = {'gre': {'10.10.10.10': '1'}}
        with mock.patch.object(
            self.agent, 'cleanup_tunnel_port'
        ) as clean_tun_fn:
            self.agent.tunnel_delete(context=None, **kwargs)
            self.assertTrue(clean_tun_fn.called)

    def test_ovs_status(self):
        reply2 = {'current': set(['tap0']),
                  'added': set(['tap2']),
                  'removed': set([])}

        reply3 = {'current': set(['tap2']),
                  'added': set([]),
                  'removed': set(['tap0'])}

        with contextlib.nested(
            mock.patch.object(async_process.AsyncProcess, "_spawn"),
            mock.patch.object(log.KeywordArgumentAdapter, 'exception'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'scan_ports'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'process_network_ports'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'check_ovs_status'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'setup_integration_br'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'setup_physical_bridges'),
            mock.patch.object(time, 'sleep'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'update_stale_ofport_rules')
        ) as (spawn_fn, log_exception, scan_ports, process_network_ports,
              check_ovs_status, setup_int_br, setup_phys_br, time_sleep,
              update_stale):
            log_exception.side_effect = Exception(
                'Fake exception to get out of the loop')
            scan_ports.side_effect = [reply2, reply3]
            process_network_ports.side_effect = [
                False, Exception('Fake exception to get out of the loop')]
            check_ovs_status.side_effect = [constants.OVS_NORMAL,
                                            constants.OVS_DEAD,
                                            constants.OVS_RESTARTED]

            # This will exit after the third loop
            try:
                self.agent.daemon_loop()
            except Exception:
                pass

        scan_ports.assert_has_calls([
            mock.call(set(), set()),
            mock.call(set(), set())
        ])
        process_network_ports.assert_has_calls([
            mock.call({'current': set(['tap0']),
                       'removed': set([]),
                       'added': set(['tap2'])}, False),
            mock.call({'current': set(['tap2']),
                       'removed': set(['tap0']),
                       'added': set([])}, True)
        ])
        self.assertTrue(update_stale.called)
        # Verify the second time through the loop we triggered an
        # OVS restart and re-setup the bridges
        setup_int_br.assert_has_calls([mock.call()])
        setup_phys_br.assert_has_calls([mock.call({})])

    def test_set_rpc_timeout(self):
        self.agent._handle_sigterm(None, None)
        for rpc_client in (self.agent.plugin_rpc.client,
                           self.agent.sg_plugin_rpc.client,
                           self.agent.dvr_plugin_rpc.client,
                           self.agent.state_rpc.client):
            self.assertEqual(10, rpc_client.timeout)

    def test_set_rpc_timeout_no_value(self):
        self.agent.quitting_rpc_timeout = None
        with mock.patch.object(self.agent, 'set_rpc_timeout') as mock_set_rpc:
            self.agent._handle_sigterm(None, None)
        self.assertFalse(mock_set_rpc.called)

    def test_arp_spoofing_disabled(self):
        self.agent.prevent_arp_spoofing = False
        # all of this is required just to get to the part of
        # treat_devices_added_or_updated that checks the prevent_arp_spoofing
        # flag
        self.agent.int_br = mock.create_autospec(self.agent.int_br)
        self.agent.treat_vif_port = mock.Mock()
        self.agent.get_vif_port_by_id = mock.Mock(return_value=FakeVif())
        self.agent.plugin_rpc = mock.Mock()
        plist = [{a: a for a in ('port_id', 'network_id', 'network_type',
                                 'physical_network', 'segmentation_id',
                                 'admin_state_up', 'fixed_ips', 'device',
                                 'device_owner')}]
        self.agent.plugin_rpc.get_devices_details_list.return_value = plist
        self.agent.setup_arp_spoofing_protection = mock.Mock()
        self.agent.treat_devices_added_or_updated([], False)
        self.assertFalse(self.agent.setup_arp_spoofing_protection.called)

    def test_arp_spoofing_port_security_disabled(self):
        int_br = mock.create_autospec(self.agent.int_br)
        self.agent.setup_arp_spoofing_protection(
            int_br, FakeVif(), {'port_security_enabled': False})
        self.assertTrue(int_br.delete_arp_spoofing_protection.called)
        self.assertFalse(int_br.install_arp_spoofing_protection.called)

    def test_arp_spoofing_basic_rule_setup(self):
        vif = FakeVif()
        fake_details = {'fixed_ips': []}
        self.agent.prevent_arp_spoofing = True
        int_br = mock.create_autospec(self.agent.int_br)
        self.agent.setup_arp_spoofing_protection(int_br, vif, fake_details)
        self.assertEqual(
            [mock.call(port=vif.ofport)],
            int_br.delete_arp_spoofing_protection.mock_calls)
        self.assertEqual(
            [mock.call(ip_addresses=set(), port=vif.ofport)],
            int_br.install_arp_spoofing_protection.mock_calls)

    def test_arp_spoofing_fixed_and_allowed_addresses(self):
        vif = FakeVif()
        fake_details = {
            'fixed_ips': [{'ip_address': '192.168.44.100'},
                          {'ip_address': '192.168.44.101'}],
            'allowed_address_pairs': [{'ip_address': '192.168.44.102/32'},
                                      {'ip_address': '192.168.44.103/32'}]
        }
        self.agent.prevent_arp_spoofing = True
        int_br = mock.create_autospec(self.agent.int_br)
        self.agent.setup_arp_spoofing_protection(int_br, vif, fake_details)
        # make sure all addresses are allowed
        addresses = {'192.168.44.100', '192.168.44.101', '192.168.44.102/32',
                     '192.168.44.103/32'}
        self.assertEqual(
            [mock.call(port=vif.ofport, ip_addresses=addresses)],
            int_br.install_arp_spoofing_protection.mock_calls)

    def test__get_ofport_moves(self):
        previous = {'port1': 1, 'port2': 2}
        current = {'port1': 5, 'port2': 2}
        # we expect it to tell us port1 moved
        expected = ['port1']
        self.assertEqual(expected,
                         self.agent._get_ofport_moves(current, previous))

    def test_update_stale_ofport_rules_clears_old(self):
        self.agent.prevent_arp_spoofing = True
        self.agent.vifname_to_ofport_map = {'port1': 1, 'port2': 2}
        self.agent.int_br = mock.Mock()
        # simulate port1 was removed
        newmap = {'port2': 2}
        self.agent.int_br.get_vif_port_to_ofport_map.return_value = newmap
        self.agent.update_stale_ofport_rules()
        # rules matching port 1 should have been deleted
        self.assertEqual(
            [mock.call(port=1)],
            self.agent.int_br.delete_arp_spoofing_protection.mock_calls)
        # make sure the state was updated with the new map
        self.assertEqual(self.agent.vifname_to_ofport_map, newmap)

    def test_update_stale_ofport_rules_treats_moved(self):
        self.agent.prevent_arp_spoofing = True
        self.agent.vifname_to_ofport_map = {'port1': 1, 'port2': 2}
        self.agent.treat_devices_added_or_updated = mock.Mock()
        self.agent.int_br = mock.Mock()
        # simulate port1 was moved
        newmap = {'port2': 2, 'port1': 90}
        self.agent.int_br.get_vif_port_to_ofport_map.return_value = newmap
        self.agent.update_stale_ofport_rules()
        self.agent.treat_devices_added_or_updated.assert_called_with(
            ['port1'], ovs_restarted=False)


class TestOvsNeutronAgentOFCtl(TestOvsNeutronAgent,
                               ovs_test_base.OVSOFCtlTestBase):
    pass


class AncillaryBridgesTest(object):

    def setUp(self):
        super(AncillaryBridgesTest, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        cfg.CONF.set_override('report_interval', 0, 'AGENT')
        self.kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

    def _test_ancillary_bridges(self, bridges, ancillary):
        device_ids = ancillary[:]

        def pullup_side_effect(*args):
            # Check that the device_id exists, if it does return it
            # if it does not return None
            try:
                device_ids.remove(args[0])
                return args[0]
            except Exception:
                return None

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                       'setup_integration_br'),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.common.ovs_lib.BaseOVS.get_bridges',
                       return_value=bridges),
            mock.patch('neutron.agent.common.ovs_lib.BaseOVS.'
                       'get_bridge_external_bridge_id',
                       side_effect=pullup_side_effect)):
            self.agent = self.mod_agent.OVSNeutronAgent(self._bridge_classes(),
                                                        **self.kwargs)
            self.assertEqual(len(ancillary), len(self.agent.ancillary_brs))
            if ancillary:
                bridges = [br.br_name for br in self.agent.ancillary_brs]
                for br in ancillary:
                    self.assertIn(br, bridges)

    def test_ancillary_bridges_single(self):
        bridges = ['br-int', 'br-ex']
        self._test_ancillary_bridges(bridges, ['br-ex'])

    def test_ancillary_bridges_none(self):
        bridges = ['br-int']
        self._test_ancillary_bridges(bridges, [])

    def test_ancillary_bridges_multiple(self):
        bridges = ['br-int', 'br-ex1', 'br-ex2']
        self._test_ancillary_bridges(bridges, ['br-ex1', 'br-ex2'])


class AncillaryBridgesTestOFCtl(AncillaryBridgesTest,
                                ovs_test_base.OVSOFCtlTestBase):
    pass


class TestOvsDvrNeutronAgent(object):

    def setUp(self):
        super(TestOvsDvrNeutronAgent, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'setup_integration_br'),
            mock.patch.object(self.mod_agent.OVSNeutronAgent,
                              'setup_ancillary_bridges',
                              return_value=[]),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.common.ovs_lib.BaseOVS.get_bridges'),
            mock.patch('neutron.openstack.common.loopingcall.'
                       'FixedIntervalLoopingCall',
                       new=MockFixedIntervalLoopingCall)):
            self.agent = self.mod_agent.OVSNeutronAgent(self._bridge_classes(),
                                                        **kwargs)
            # set back to true because initial report state will succeed due
            # to mocked out RPC calls
            self.agent.use_call = True
            self.agent.tun_br = self.br_tun_cls(br_name='br-tun')
        self.agent.sg_agent = mock.Mock()

    def _setup_for_dvr_test(self, ofport=10):
        self._port = mock.Mock()
        self._port.ofport = ofport
        self._port.vif_id = "1234-5678-90"
        self._physical_network = 'physeth1'
        self._old_local_vlan = None
        self._segmentation_id = 2001
        self.agent.enable_distributed_routing = True
        self.agent.enable_tunneling = True
        self.agent.patch_tun_ofport = 1
        self.agent.patch_int_ofport = 2
        self.agent.dvr_agent.local_ports = {}
        self.agent.local_vlan_map = {}
        self.agent.dvr_agent.enable_distributed_routing = True
        self.agent.dvr_agent.enable_tunneling = True
        self.agent.dvr_agent.patch_tun_ofport = 1
        self.agent.dvr_agent.patch_int_ofport = 2
        self.agent.dvr_agent.tun_br = mock.Mock()
        self.agent.dvr_agent.phys_brs[self._physical_network] = mock.Mock()
        self.agent.dvr_agent.bridge_mappings = {self._physical_network:
                                                'br-eth1'}
        self.agent.dvr_agent.int_ofports[self._physical_network] = 30
        self.agent.dvr_agent.phys_ofports[self._physical_network] = 40
        self.agent.dvr_agent.local_dvr_map = {}
        self.agent.dvr_agent.registered_dvr_macs = set()
        self.agent.dvr_agent.dvr_mac_address = 'aa:22:33:44:55:66'
        self._net_uuid = 'my-net-uuid'
        self._fixed_ips = [{'subnet_id': 'my-subnet-uuid',
                            'ip_address': '1.1.1.1'}]
        self._compute_port = mock.Mock()
        self._compute_port.ofport = 20
        self._compute_port.vif_id = "1234-5678-91"
        self._compute_fixed_ips = [{'subnet_id': 'my-subnet-uuid',
                                    'ip_address': '1.1.1.3'}]

    @staticmethod
    def _expected_port_bound(port, lvid):
        return [
            mock.call.db_get_val('Port', port.port_name, 'tag'),
            mock.call.set_db_attribute('Port', port.port_name, 'tag', lvid),
            mock.call.delete_flows(in_port=port.ofport),
        ]

    def _expected_install_dvr_process(self, lvid, port, ip_version,
                                      gateway_ip, gateway_mac):
        if ip_version == 4:
            ipvx_calls = [
                mock.call.install_dvr_process_ipv4(
                    vlan_tag=lvid,
                    gateway_ip=gateway_ip),
            ]
        else:
            ipvx_calls = [
                mock.call.install_dvr_process_ipv6(
                    vlan_tag=lvid,
                    gateway_mac=gateway_mac),
            ]
        return ipvx_calls + [
            mock.call.install_dvr_process(
                vlan_tag=lvid,
                dvr_mac_address=self.agent.dvr_agent.dvr_mac_address,
                vif_mac=port.vif_mac,
            ),
        ]

    def _test_port_bound_for_dvr_on_vlan_network(self, device_owner,
                                                 ip_version=4):
        self._setup_for_dvr_test()
        if ip_version == 4:
            gateway_ip = '1.1.1.1'
            cidr = '1.1.1.0/24'
        else:
            gateway_ip = '2001:100::1'
            cidr = '2001:100::0/64'
        self._port.vif_mac = gateway_mac = 'aa:bb:cc:11:22:33'
        self._compute_port.vif_mac = '77:88:99:00:11:22'
        physical_network = self._physical_network
        segmentation_id = self._segmentation_id
        network_type = p_const.TYPE_VLAN
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        phys_br = mock.create_autospec(self.br_phys_cls('br-phys'))
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                              'get_subnet_for_dvr',
                              return_value={
                                  'gateway_ip': gateway_ip,
                                  'cidr': cidr,
                                  'ip_version': ip_version,
                                  'gateway_mac': gateway_mac}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.phys_brs,
                            {physical_network: phys_br}),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.dvr_agent.phys_brs,
                            {physical_network: phys_br}),
        ) as (get_subnet_fn, get_cphost_fn, get_vif_fn, _, _, _, _, _, _):
            self.agent.port_bound(
                self._port, self._net_uuid, network_type,
                physical_network, segmentation_id, self._fixed_ips,
                n_const.DEVICE_OWNER_DVR_INTERFACE, False)
            phy_ofp = self.agent.dvr_agent.phys_ofports[physical_network]
            int_ofp = self.agent.dvr_agent.int_ofports[physical_network]
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            expected_on_phys_br = [
                mock.call.provision_local_vlan(
                    port=phy_ofp,
                    lvid=lvid,
                    segmentation_id=segmentation_id,
                    distributed=True,
                ),
            ] + self._expected_install_dvr_process(
                port=self._port,
                lvid=lvid,
                ip_version=ip_version,
                gateway_ip=gateway_ip,
                gateway_mac=gateway_mac)
            expected_on_int_br = [
                mock.call.provision_local_vlan(
                    port=int_ofp,
                    lvid=lvid,
                    segmentation_id=segmentation_id,
                ),
            ] + self._expected_port_bound(self._port, lvid)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual([], tun_br.mock_calls)
            self.assertEqual(expected_on_phys_br, phys_br.mock_calls)
            int_br.reset_mock()
            tun_br.reset_mock()
            phys_br.reset_mock()
            self.agent.port_bound(self._compute_port, self._net_uuid,
                                  network_type, physical_network,
                                  segmentation_id,
                                  self._compute_fixed_ips,
                                  device_owner, False)
            expected_on_int_br = [
                mock.call.install_dvr_to_src_mac(
                    network_type=network_type,
                    gateway_mac=gateway_mac,
                    dst_mac=self._compute_port.vif_mac,
                    dst_port=self._compute_port.ofport,
                    vlan_tag=segmentation_id,
                ),
            ] + self._expected_port_bound(self._compute_port, lvid)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertFalse([], tun_br.mock_calls)
            self.assertFalse([], phys_br.mock_calls)

    def _test_port_bound_for_dvr_on_vxlan_network(self, device_owner,
                                                  ip_version=4):
        self._setup_for_dvr_test()
        if ip_version == 4:
            gateway_ip = '1.1.1.1'
            cidr = '1.1.1.0/24'
        else:
            gateway_ip = '2001:100::1'
            cidr = '2001:100::0/64'
        network_type = p_const.TYPE_VXLAN
        self._port.vif_mac = gateway_mac = 'aa:bb:cc:11:22:33'
        self._compute_port.vif_mac = '77:88:99:00:11:22'
        physical_network = self._physical_network
        segmentation_id = self._segmentation_id
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        phys_br = mock.create_autospec(self.br_phys_cls('br-phys'))
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                              'get_subnet_for_dvr',
                              return_value={
                                  'gateway_ip': gateway_ip,
                                  'cidr': cidr,
                                  'ip_version': ip_version,
                                  'gateway_mac': gateway_mac}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.phys_brs,
                            {physical_network: phys_br}),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.dvr_agent.phys_brs,
                            {physical_network: phys_br}),
        ) as (get_subnet_fn, get_cphost_fn, get_vif_fn, _, _, _, _, _, _):
            self.agent.port_bound(
                self._port, self._net_uuid, network_type,
                physical_network, segmentation_id, self._fixed_ips,
                n_const.DEVICE_OWNER_DVR_INTERFACE, False)
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            expected_on_int_br = self._expected_port_bound(
                self._port, lvid)
            expected_on_tun_br = [
                mock.call.provision_local_vlan(
                    network_type=network_type,
                    segmentation_id=segmentation_id,
                    lvid=lvid,
                    distributed=True),
            ] + self._expected_install_dvr_process(
                port=self._port,
                lvid=lvid,
                ip_version=ip_version,
                gateway_ip=gateway_ip,
                gateway_mac=gateway_mac)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)
            self.assertEqual([], phys_br.mock_calls)
            int_br.reset_mock()
            tun_br.reset_mock()
            phys_br.reset_mock()
            self.agent.port_bound(self._compute_port, self._net_uuid,
                                  network_type, physical_network,
                                  segmentation_id,
                                  self._compute_fixed_ips,
                                  device_owner, False)
            expected_on_int_br = [
                mock.call.install_dvr_to_src_mac(
                    network_type=network_type,
                    gateway_mac=gateway_mac,
                    dst_mac=self._compute_port.vif_mac,
                    dst_port=self._compute_port.ofport,
                    vlan_tag=lvid,
                ),
            ] + self._expected_port_bound(self._compute_port, lvid)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual([], tun_br.mock_calls)
            self.assertEqual([], phys_br.mock_calls)

    def test_port_bound_for_dvr_with_compute_ports(self):
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner="compute:None")
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner="compute:None", ip_version=6)
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner="compute:None")
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner="compute:None", ip_version=6)

    def test_port_bound_for_dvr_with_lbaas_vip_ports(self):
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER)
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER, ip_version=6)
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER)
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER, ip_version=6)

    def test_port_bound_for_dvr_with_dhcp_ports(self):
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner=n_const.DEVICE_OWNER_DHCP)
        self._test_port_bound_for_dvr_on_vlan_network(
            device_owner=n_const.DEVICE_OWNER_DHCP, ip_version=6)
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner=n_const.DEVICE_OWNER_DHCP)
        self._test_port_bound_for_dvr_on_vxlan_network(
            device_owner=n_const.DEVICE_OWNER_DHCP, ip_version=6)

    def test_port_bound_for_dvr_with_csnat_ports(self, ofport=10):
        self._setup_for_dvr_test()
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(
                self.agent.dvr_agent.plugin_rpc, 'get_subnet_for_dvr',
                return_value={'gateway_ip': '1.1.1.1',
                              'cidr': '1.1.1.0/24',
                              'ip_version': 4,
                              'gateway_mac': 'aa:bb:cc:11:22:33'}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (get_subnet_fn, get_cphost_fn, get_vif_fn, _, _, _, _):
            self.agent.port_bound(
                self._port, self._net_uuid, 'vxlan',
                None, None, self._fixed_ips,
                n_const.DEVICE_OWNER_ROUTER_SNAT,
                False)
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            expected_on_int_br = [
                mock.call.install_dvr_to_src_mac(
                    network_type='vxlan',
                    gateway_mac='aa:bb:cc:11:22:33',
                    dst_mac=self._port.vif_mac,
                    dst_port=self._port.ofport,
                    vlan_tag=lvid,
                ),
            ] + self._expected_port_bound(self._port, lvid)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            expected_on_tun_br = [
                mock.call.provision_local_vlan(
                    network_type='vxlan',
                    lvid=lvid,
                    segmentation_id=None,
                    distributed=True,
                ),
            ]
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)

    def test_treat_devices_removed_for_dvr_interface(self, ofport=10):
        self._test_treat_devices_removed_for_dvr_interface(ofport)
        self._test_treat_devices_removed_for_dvr_interface(
            ofport, ip_version=6)

    def _test_treat_devices_removed_for_dvr_interface(self, ofport=10,
                                                      ip_version=4):
        self._setup_for_dvr_test()
        if ip_version == 4:
            gateway_ip = '1.1.1.1'
            cidr = '1.1.1.0/24'
        else:
            gateway_ip = '2001:100::1'
            cidr = '2001:100::0/64'
        gateway_mac = 'aa:bb:cc:11:22:33'
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(
                self.agent.dvr_agent.plugin_rpc, 'get_subnet_for_dvr',
                return_value={'gateway_ip': gateway_ip,
                              'cidr': cidr,
                              'ip_version': ip_version,
                              'gateway_mac': gateway_mac}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
        ) as (get_subnet_fn, get_cphost_fn, _, _, _, _, get_vif_fn):
            self.agent.port_bound(
                self._port, self._net_uuid, 'vxlan',
                None, None, self._fixed_ips,
                n_const.DEVICE_OWNER_DVR_INTERFACE,
                False)
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            self.assertEqual(self._expected_port_bound(self._port, lvid),
                             int_br.mock_calls)
            expected_on_tun_br = [
                mock.call.provision_local_vlan(network_type='vxlan',
                    lvid=lvid, segmentation_id=None, distributed=True),
            ] + self._expected_install_dvr_process(
                port=self._port,
                lvid=lvid,
                ip_version=ip_version,
                gateway_ip=gateway_ip,
                gateway_mac=gateway_mac)
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)

        int_br.reset_mock()
        tun_br.reset_mock()
        with contextlib.nested(
            mock.patch.object(self.agent, 'reclaim_local_vlan'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                              return_value=None),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (reclaim_vlan_fn, update_dev_down_fn, _, _, _, _):
                self.agent.treat_devices_removed([self._port.vif_id])
                if ip_version == 4:
                    expected = [
                        mock.call.delete_dvr_process_ipv4(
                            vlan_tag=lvid,
                            gateway_ip=gateway_ip),
                    ]
                else:
                    expected = [
                        mock.call.delete_dvr_process_ipv6(
                            vlan_tag=lvid,
                            gateway_mac=gateway_mac),
                    ]
                expected.extend([
                    mock.call.delete_dvr_process(
                        vlan_tag=lvid,
                        vif_mac=self._port.vif_mac),
                ])
                self.assertEqual([], int_br.mock_calls)
                self.assertEqual(expected, tun_br.mock_calls)

    def _test_treat_devices_removed_for_dvr(self, device_owner, ip_version=4):
        self._setup_for_dvr_test()
        if ip_version == 4:
            gateway_ip = '1.1.1.1'
            cidr = '1.1.1.0/24'
        else:
            gateway_ip = '2001:100::1'
            cidr = '2001:100::0/64'
        gateway_mac = 'aa:bb:cc:11:22:33'
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(
                self.agent.dvr_agent.plugin_rpc, 'get_subnet_for_dvr',
                return_value={'gateway_ip': gateway_ip,
                              'cidr': cidr,
                              'ip_version': ip_version,
                              'gateway_mac': gateway_mac}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (get_subnet_fn, get_cphost_fn, get_vif_fn, _, _, _, _):
            self.agent.port_bound(
                self._port, self._net_uuid, 'vxlan',
                None, None, self._fixed_ips,
                n_const.DEVICE_OWNER_DVR_INTERFACE,
                False)
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            self.assertEqual(
                self._expected_port_bound(self._port, lvid),
                int_br.mock_calls)
            expected_on_tun_br = [
                mock.call.provision_local_vlan(
                    network_type='vxlan',
                    segmentation_id=None,
                    lvid=lvid,
                    distributed=True),
            ] + self._expected_install_dvr_process(
                port=self._port,
                lvid=lvid,
                ip_version=ip_version,
                gateway_ip=gateway_ip,
                gateway_mac=gateway_mac)
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)
            int_br.reset_mock()
            tun_br.reset_mock()
            self.agent.port_bound(self._compute_port,
                                  self._net_uuid, 'vxlan',
                                  None, None,
                                  self._compute_fixed_ips,
                                  device_owner, False)
            self.assertEqual(
                [
                    mock.call.install_dvr_to_src_mac(
                        network_type='vxlan',
                        gateway_mac='aa:bb:cc:11:22:33',
                        dst_mac=self._compute_port.vif_mac,
                        dst_port=self._compute_port.ofport,
                        vlan_tag=lvid,
                    ),
                ] + self._expected_port_bound(self._compute_port, lvid),
                int_br.mock_calls)
            self.assertEqual([], tun_br.mock_calls)

        int_br.reset_mock()
        tun_br.reset_mock()
        with contextlib.nested(
            mock.patch.object(self.agent, 'reclaim_local_vlan'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                              return_value=None),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (reclaim_vlan_fn, update_dev_down_fn, _, _, _, _):
                self.agent.treat_devices_removed([self._compute_port.vif_id])
                int_br.assert_has_calls([
                    mock.call.delete_dvr_to_src_mac(
                        network_type='vxlan',
                        vlan_tag=lvid,
                        dst_mac=self._compute_port.vif_mac,
                    ),
                ])
                self.assertEqual([], tun_br.mock_calls)

    def test_treat_devices_removed_for_dvr_with_compute_ports(self):
        self._test_treat_devices_removed_for_dvr(
            device_owner="compute:None")
        self._test_treat_devices_removed_for_dvr(
            device_owner="compute:None", ip_version=6)

    def test_treat_devices_removed_for_dvr_with_lbaas_vip_ports(self):
        self._test_treat_devices_removed_for_dvr(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER)
        self._test_treat_devices_removed_for_dvr(
            device_owner=n_const.DEVICE_OWNER_LOADBALANCER, ip_version=6)

    def test_treat_devices_removed_for_dvr_with_dhcp_ports(self):
        self._test_treat_devices_removed_for_dvr(
            device_owner=n_const.DEVICE_OWNER_DHCP)
        self._test_treat_devices_removed_for_dvr(
            device_owner=n_const.DEVICE_OWNER_DHCP, ip_version=6)

    def test_treat_devices_removed_for_dvr_csnat_port(self, ofport=10):
        self._setup_for_dvr_test()
        gateway_mac = 'aa:bb:cc:11:22:33'
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        int_br.set_db_attribute.return_value = True
        int_br.db_get_val.return_value = self._old_local_vlan
        with contextlib.nested(
            mock.patch.object(
                self.agent.dvr_agent.plugin_rpc, 'get_subnet_for_dvr',
                return_value={'gateway_ip': '1.1.1.1',
                              'cidr': '1.1.1.0/24',
                              'ip_version': 4,
                              'gateway_mac': gateway_mac}),
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                'get_ports_on_host_by_subnet',
                return_value=[]),
            mock.patch.object(self.agent.dvr_agent.int_br,
                              'get_vif_port_by_id',
                              return_value=self._port),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (get_subnet_fn, get_cphost_fn, get_vif_fn, _, _, _, _):
            self.agent.port_bound(
                self._port, self._net_uuid, 'vxlan',
                None, None, self._fixed_ips,
                n_const.DEVICE_OWNER_ROUTER_SNAT,
                False)
            lvid = self.agent.local_vlan_map[self._net_uuid].vlan
            expected_on_int_br = [
                mock.call.install_dvr_to_src_mac(
                    network_type='vxlan',
                    gateway_mac=gateway_mac,
                    dst_mac=self._port.vif_mac,
                    dst_port=self._port.ofport,
                    vlan_tag=lvid,
                ),
            ] + self._expected_port_bound(self._port, lvid)
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            expected_on_tun_br = [
                mock.call.provision_local_vlan(
                    network_type='vxlan',
                    lvid=lvid,
                    segmentation_id=None,
                    distributed=True,
                ),
            ]
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)

        int_br.reset_mock()
        tun_br.reset_mock()
        with contextlib.nested(
            mock.patch.object(self.agent, 'reclaim_local_vlan'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                              return_value=None),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
        ) as (reclaim_vlan_fn, update_dev_down_fn, _, _, _, _):
                self.agent.treat_devices_removed([self._port.vif_id])
                expected_on_int_br = [
                    mock.call.delete_dvr_to_src_mac(
                        network_type='vxlan',
                        dst_mac=self._port.vif_mac,
                        vlan_tag=lvid,
                    ),
                ]
                self.assertEqual(expected_on_int_br, int_br.mock_calls)
                expected_on_tun_br = []
                self.assertEqual(expected_on_tun_br, tun_br.mock_calls)

    def test_setup_dvr_flows_on_int_br(self):
        self._setup_for_dvr_test()
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        with contextlib.nested(
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.object(
                self.agent.dvr_agent.plugin_rpc,
                'get_dvr_mac_address_list',
                return_value=[{'host': 'cn1',
                               'mac_address': 'aa:bb:cc:dd:ee:ff'},
                              {'host': 'cn2',
                               'mac_address': '11:22:33:44:55:66'}])
        ) as (_, _, _, _, get_mac_list_fn):
            self.agent.dvr_agent.setup_dvr_flows_on_integ_br()
            self.assertTrue(self.agent.dvr_agent.in_distributed_mode())
            physical_networks = self.agent.dvr_agent.bridge_mappings.keys()
            ioport = self.agent.dvr_agent.int_ofports[physical_networks[0]]
            expected_on_int_br = [
                # setup_dvr_flows_on_integ_br
                mock.call.delete_flows(),
                mock.call.setup_canary_table(),
                mock.call.install_drop(table_id=constants.DVR_TO_SRC_MAC,
                                       priority=1),
                mock.call.install_drop(table_id=constants.DVR_TO_SRC_MAC_VLAN,
                                       priority=1),
                mock.call.install_normal(table_id=constants.LOCAL_SWITCHING,
                                         priority=1),
                mock.call.install_drop(table_id=constants.LOCAL_SWITCHING,
                                       priority=2,
                                       in_port=ioport),
            ]
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual([], tun_br.mock_calls)

    def test_get_dvr_mac_address(self):
        self._setup_for_dvr_test()
        self.agent.dvr_agent.dvr_mac_address = None
        with mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                               'get_dvr_mac_address_by_host',
                               return_value={'host': 'cn1',
                                  'mac_address': 'aa:22:33:44:55:66'}):
            self.agent.dvr_agent.get_dvr_mac_address()
            self.assertEqual('aa:22:33:44:55:66',
                             self.agent.dvr_agent.dvr_mac_address)
            self.assertTrue(self.agent.dvr_agent.in_distributed_mode())

    def test_get_dvr_mac_address_exception(self):
        self._setup_for_dvr_test()
        self.agent.dvr_agent.dvr_mac_address = None
        int_br = mock.create_autospec(self.agent.int_br)
        with contextlib.nested(
                mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                               'get_dvr_mac_address_by_host',
                               side_effect=oslo_messaging.RemoteError),
                mock.patch.object(self.agent, 'int_br', new=int_br),
                mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
        ) as (gd_mac, _, _):
            self.agent.dvr_agent.get_dvr_mac_address()
            self.assertIsNone(self.agent.dvr_agent.dvr_mac_address)
            self.assertFalse(self.agent.dvr_agent.in_distributed_mode())
            self.assertEqual([mock.call.install_normal()], int_br.mock_calls)

    def test_get_dvr_mac_address_retried(self):
        valid_entry = {'host': 'cn1', 'mac_address': 'aa:22:33:44:55:66'}
        raise_timeout = oslo_messaging.MessagingTimeout()
        # Raise a timeout the first 2 times it calls get_dvr_mac_address()
        self._setup_for_dvr_test()
        self.agent.dvr_agent.dvr_mac_address = None
        with mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                               'get_dvr_mac_address_by_host',
                               side_effect=(raise_timeout, raise_timeout,
                                            valid_entry)):
            self.agent.dvr_agent.get_dvr_mac_address()
            self.assertEqual('aa:22:33:44:55:66',
                             self.agent.dvr_agent.dvr_mac_address)
            self.assertTrue(self.agent.dvr_agent.in_distributed_mode())
            self.assertEqual(self.agent.dvr_agent.plugin_rpc.
                             get_dvr_mac_address_by_host.call_count, 3)

    def test_get_dvr_mac_address_retried_max(self):
        raise_timeout = oslo_messaging.MessagingTimeout()
        # Raise a timeout every time until we give up, currently 5 tries
        self._setup_for_dvr_test()
        self.agent.dvr_agent.dvr_mac_address = None
        int_br = mock.create_autospec(self.agent.int_br)
        with contextlib.nested(
            mock.patch.object(self.agent.dvr_agent.plugin_rpc,
                             'get_dvr_mac_address_by_host',
                             side_effect=raise_timeout),
            mock.patch.object(utils, "execute"),
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
        ) as (rpc_mock, execute_mock, _, _):
            self.agent.dvr_agent.get_dvr_mac_address()
            self.assertIsNone(self.agent.dvr_agent.dvr_mac_address)
            self.assertFalse(self.agent.dvr_agent.in_distributed_mode())
            self.assertEqual(self.agent.dvr_agent.plugin_rpc.
                             get_dvr_mac_address_by_host.call_count, 5)

    def test_dvr_mac_address_update(self):
        self._setup_for_dvr_test()
        newhost = 'cn2'
        newmac = 'aa:bb:cc:dd:ee:ff'
        int_br = mock.create_autospec(self.agent.int_br)
        tun_br = mock.create_autospec(self.agent.tun_br)
        phys_br = mock.create_autospec(self.br_phys_cls('br-phys'))
        physical_network = 'physeth1'
        with contextlib.nested(
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.phys_brs,
                            {physical_network: phys_br}),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.dvr_agent.phys_brs,
                            {physical_network: phys_br}),
        ):
            self.agent.dvr_agent.\
                dvr_mac_address_update(
                    dvr_macs=[{'host': newhost,
                               'mac_address': newmac}])
            expected_on_int_br = [
                mock.call.add_dvr_mac_vlan(
                    mac=newmac,
                    port=self.agent.int_ofports[physical_network]),
                mock.call.add_dvr_mac_tun(
                    mac=newmac,
                    port=self.agent.patch_tun_ofport),
            ]
            expected_on_tun_br = [
                mock.call.add_dvr_mac_tun(
                    mac=newmac,
                    port=self.agent.patch_int_ofport),
            ]
            expected_on_phys_br = [
                mock.call.add_dvr_mac_vlan(
                    mac=newmac,
                    port=self.agent.phys_ofports[physical_network]),
            ]
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)
            self.assertEqual(expected_on_phys_br, phys_br.mock_calls)
        int_br.reset_mock()
        tun_br.reset_mock()
        phys_br.reset_mock()
        with contextlib.nested(
            mock.patch.object(self.agent, 'int_br', new=int_br),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.phys_brs,
                            {physical_network: phys_br}),
            mock.patch.object(self.agent.dvr_agent, 'int_br', new=int_br),
            mock.patch.object(self.agent.dvr_agent, 'tun_br', new=tun_br),
            mock.patch.dict(self.agent.dvr_agent.phys_brs,
                            {physical_network: phys_br}),
        ):
            self.agent.dvr_agent.dvr_mac_address_update(dvr_macs=[])
            expected_on_int_br = [
                mock.call.remove_dvr_mac_vlan(
                    mac=newmac),
                mock.call.remove_dvr_mac_tun(
                    mac=newmac,
                    port=self.agent.patch_tun_ofport),
            ]
            expected_on_tun_br = [
                mock.call.remove_dvr_mac_tun(
                    mac=newmac),
            ]
            expected_on_phys_br = [
                mock.call.remove_dvr_mac_vlan(
                    mac=newmac),
            ]
            self.assertEqual(expected_on_int_br, int_br.mock_calls)
            self.assertEqual(expected_on_tun_br, tun_br.mock_calls)
            self.assertEqual(expected_on_phys_br, phys_br.mock_calls)

    def test_ovs_restart(self):
        self._setup_for_dvr_test()
        reset_methods = (
            'reset_ovs_parameters', 'reset_dvr_parameters',
            'setup_dvr_flows_on_integ_br', 'setup_dvr_flows_on_tun_br',
            'setup_dvr_flows_on_phys_br', 'setup_dvr_mac_flows_on_all_brs')
        reset_mocks = [mock.patch.object(self.agent.dvr_agent, method).start()
                       for method in reset_methods]
        tun_br = mock.create_autospec(self.agent.tun_br)
        with contextlib.nested(
            mock.patch.object(self.agent, 'check_ovs_status',
                              return_value=constants.OVS_RESTARTED),
            mock.patch.object(self.agent, '_agent_has_updates',
                              side_effect=TypeError('loop exit')),
            mock.patch.object(self.agent, 'tun_br', new=tun_br),
        ):
            # block RPC calls and bridge calls
            self.agent.setup_physical_bridges = mock.Mock()
            self.agent.setup_integration_br = mock.Mock()
            self.agent.reset_tunnel_br = mock.Mock()
            self.agent.state_rpc = mock.Mock()
            try:
                self.agent.rpc_loop(polling_manager=mock.Mock())
            except TypeError:
                pass
        self.assertTrue(all([x.called for x in reset_mocks]))


class TestOvsDvrNeutronAgentOFCtl(TestOvsDvrNeutronAgent,
                                  ovs_test_base.OVSOFCtlTestBase):
    pass
