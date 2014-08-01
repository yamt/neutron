# Copyright (C) 2014 VA Linux Systems Japan K.K.
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
#
# @author: Fumihiko Kakuma, VA Linux Systems Japan K.K.
# @author: YAMAMOTO Takashi, VA Linux Systems Japan K.K.

import mock
from oslo.config import cfg

from neutron.openstack.common import importutils
from neutron.tests import base
from neutron.tests.unit.ofagent import fake_oflib


class OFAAgentTestBase(base.BaseTestCase):

    _AGENT_NAME = 'neutron.plugins.ofagent.agent.ofa_neutron_agent'

    def setUp(self):
        self.fake_oflib_of = fake_oflib.patch_fake_oflib_of()
        self.fake_oflib_of.start()
        self.addCleanup(self.fake_oflib_of.stop)
        self.mod_agent = importutils.import_module(self._AGENT_NAME)
        super(OFAAgentTestBase, self).setUp()
        self.ryuapp = mock.Mock()

    def setup_config(self):
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        cfg.CONF.register_cli_opts([
            cfg.StrOpt('ofp-listen-host', default='',
                       help='openflow listen host'),
            cfg.IntOpt('ofp-tcp-listen-port', default=6633,
                       help='openflow tcp listen port')
        ])
        cfg.CONF.set_override('root_helper', 'fake_helper', group='AGENT')

    def _mk_test_dp(self, name):
        ofp = importutils.import_module('ryu.ofproto.ofproto_v1_3')
        ofpp = importutils.import_module('ryu.ofproto.ofproto_v1_3_parser')
        dp = mock.Mock()
        dp.ofproto = ofp
        dp.ofproto_parser = ofpp
        dp.__repr__ = mock.Mock(return_value=name)
        return dp

    def _mk_test_br(self, name):
        dp = self._mk_test_dp(name)
        br = mock.Mock()
        br.datapath = dp
        br.ofproto = dp.ofproto
        br.ofparser = dp.ofproto_parser
        return br
