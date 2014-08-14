# Copyright (c) 2014 OpenStack Foundation
# All Rights Reserved.
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

from neutron.common import constants
from neutron.extensions import portbindings
from neutron.plugins.ml2.drivers import mech_ofagent
from neutron.tests.unit.ml2 import _test_mech_agent as base


class OfagentMechanismBaseTestCase(base.AgentMechanismBaseTestCase):
    VIF_TYPE = portbindings.VIF_TYPE_OVS
    CAP_PORT_FILTER = True
    AGENT_TYPE = constants.AGENT_TYPE_OFA

    GOOD_MAPPINGS = {'fake_physical_network': 'fake_interface'}
    GOOD_TUNNEL_TYPES = ['gre', 'vxlan']
    GOOD_CONFIGS = {'interface_mappings': GOOD_MAPPINGS,
                    'tunnel_types': GOOD_TUNNEL_TYPES}

    BAD_MAPPINGS = {'wrong_physical_network': 'wrong_interface'}
    BAD_TUNNEL_TYPES = ['bad_tunnel_type']
    BAD_CONFIGS = {'interface_mappings': BAD_MAPPINGS,
                   'tunnel_types': BAD_TUNNEL_TYPES}

    AGENTS = [{'alive': True,
               'configurations': GOOD_CONFIGS}]
    AGENTS_DEAD = [{'alive': False,
                    'configurations': GOOD_CONFIGS}]
    AGENTS_BAD = [{'alive': False,
                   'configurations': GOOD_CONFIGS},
                  {'alive': True,
                   'configurations': BAD_CONFIGS}]

    def setUp(self):
        super(OfagentMechanismBaseTestCase, self).setUp()
        self.driver = mech_ofagent.OfagentMechanismDriver()
        self.driver.initialize()


class OfagentMechanismGenericTestCase(OfagentMechanismBaseTestCase,
                                      base.AgentMechanismGenericTestCase):
    pass


class OfagentMechanismLocalTestCase(OfagentMechanismBaseTestCase,
                                    base.AgentMechanismLocalTestCase):
    pass


class OfagentMechanismFlatTestCase(OfagentMechanismBaseTestCase,
                                   base.AgentMechanismFlatTestCase):
    pass


class OfagentMechanismVlanTestCase(OfagentMechanismBaseTestCase,
                                   base.AgentMechanismVlanTestCase):
    pass


class OfagentMechanismGreTestCase(OfagentMechanismBaseTestCase,
                                  base.AgentMechanismGreTestCase):
    pass


# The following tests are for deprecated "bridge_mappings".
# TODO(yamamoto): Remove them.

class OfagentMechanismPhysBridgeTestCase(base.AgentMechanismBaseTestCase):
    VIF_TYPE = portbindings.VIF_TYPE_OVS
    CAP_PORT_FILTER = True
    AGENT_TYPE = constants.AGENT_TYPE_OFA

    GOOD_MAPPINGS = {'fake_physical_network': 'fake_bridge'}
    GOOD_TUNNEL_TYPES = ['gre', 'vxlan']
    GOOD_CONFIGS = {'bridge_mappings': GOOD_MAPPINGS,
                    'tunnel_types': GOOD_TUNNEL_TYPES}

    BAD_MAPPINGS = {'wrong_physical_network': 'wrong_bridge'}
    BAD_TUNNEL_TYPES = ['bad_tunnel_type']
    BAD_CONFIGS = {'bridge_mappings': BAD_MAPPINGS,
                   'tunnel_types': BAD_TUNNEL_TYPES}

    AGENTS = [{'alive': True,
               'configurations': GOOD_CONFIGS}]
    AGENTS_DEAD = [{'alive': False,
                    'configurations': GOOD_CONFIGS}]
    AGENTS_BAD = [{'alive': False,
                   'configurations': GOOD_CONFIGS},
                  {'alive': True,
                   'configurations': BAD_CONFIGS}]

    def setUp(self):
        super(OfagentMechanismPhysBridgeTestCase, self).setUp()
        self.driver = mech_ofagent.OfagentMechanismDriver()
        self.driver.initialize()


class OfagentMechanismPhysBridgeGenericTestCase(
        OfagentMechanismPhysBridgeTestCase,
        base.AgentMechanismGenericTestCase):
    pass


class OfagentMechanismPhysBridgeLocalTestCase(
        OfagentMechanismPhysBridgeTestCase,
        base.AgentMechanismLocalTestCase):
    pass


class OfagentMechanismPhysBridgeFlatTestCase(
        OfagentMechanismPhysBridgeTestCase,
        base.AgentMechanismFlatTestCase):
    pass


class OfagentMechanismPhysBridgeVlanTestCase(
        OfagentMechanismPhysBridgeTestCase,
        base.AgentMechanismVlanTestCase):
    pass


class OfagentMechanismPhysBridgeGreTestCase(
        OfagentMechanismPhysBridgeTestCase,
        base.AgentMechanismGreTestCase):
    pass
