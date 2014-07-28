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
# @author: YAMAMOTO Takashi, VA Linux Systems Japan K.K.

"""
OpenFlow1.3 flow table for OFAgent

* requirements
** plain OpenFlow 1.3. no vendor extensions.

* todo: VXLAN (same as GRE?)
* todo: what to do for mpnet?

* legends
 xxx: network id  (agent internal use)
 yyy: segment id  (vlan id, gre key, ...)
 a,b,c: tunnel port  (tun_ofports, map[net_id].tun_ofports)
 i,j,k: vm port  (map[net_id].vif_ports[vif_id].ofport)
 x,y,z: physical port  (int_ofports)
 N: tunnel type  (0 for TYPE_GRE, 1 for TYPE_xxx, ...)
 uuu: unicast l2 address

* tables (in order)
    CHECK_IN_PORT
    TUNNEL_IN+N
    PHYS_IN
    LOCAL_IN
    TUNNEL_OUT
    LOCAL_OUT
    PHYS_OUT
    TUNNEL_FLOOD+N
    PHYS_FLOOD
    LOCAL_FLOOD

* CHECK_IN_PORT

   for each vm ports:
      // check_in_port_add_local_port, check_in_port_delete_port
      in_port=i, write_metadata(xxx),goto(LOCAL_IN)
   TYPE_GRE
   for each tunnel ports:
      // check_in_port_add_tunnel_port, check_in_port_delete_port
      in_port=a, goto(TUNNEL_IN+N)
   TYPE_VLAN
   for each networks ports:
      // provision_tenant_physnet, reclaim_tenant_physnet
      in_port=x,vlan_vid=present|yyy, write_metadata(xxx),goto(PHYS_IN)
   TYPE_FLAT
      // provision_tenant_physnet, reclaim_tenant_physnet
      in_port=x, write_metadata(xxx),goto(PHYS_IN)
   default drop

* TUNNEL_IN+N  (per tunnel types)  tunnel -> network

   for each networks:
      // provision_tenant_tunnel, reclaim_tenant_tunnel
      // don't goto(TUNNEL_OUT) as it can create a loop with meshed tunnels
      // what to do when using multiple tunnel types?
      tun_id=yyy, write_metadata(xxx),goto(PHYS_OUT)

   default drop

* PHYS_IN
   default goto(TUNNEL_OUT)

* LOCAL_IN
** todo: local arp responder

   default goto(next_table)

* TUNNEL_OUT
   TYPE_GRE
   // !FLOODING_ENTRY
   // install_tunnel_output, delete_tunnel_output
   metadata=xxx,eth_dst=uuu  set_tunnel(yyy),output:a

   default goto(next table)

* LOCAL_OUT
** todo: probably make get_device_details to return vm mac address?

   for each known destinations:
      // local_out_add_port, local_out_delete_port
      metadata=xxx,eth_dst=uuu output:i
   default goto(next table)

* PHYS_OUT
** todo: learning and/or l2 pop

   for each known destinations:  (is this even possible for VLAN???)
       TYPE_VLAN
       metadata=xxx,eth_dst=uuu  push_vlan,set_field:present|yyy->vlan_vid,output:a
   default goto(next table)

* TUNNEL_FLOOD+N. (per tunnel types)

   network -> tunnel/vlan
   output to tunnel/physical ports
   "next table" might be LOCAL_OUT
   TYPE_GRE
   for each networks:
      // FLOODING_ENTRY
      // install_tunnel_output, delete_tunnel_output
      metadata=xxx, set_tunnel(yyy),output:a,b,c,goto(next table)

   default goto(next table)

* PHYS_FLOOD

   TYPE_VLAN
   for each networks:
      // provision_tenant_physnet, reclaim_tenant_physnet
      metadata=xxx, push_vlan:0x8100,set_field:present|yyy->vlan_vid,output:x,pop_vlan,goto(next table)
   TYPE_FLAT
   for each networks:
      // provision_tenant_physnet, reclaim_tenant_physnet
      metadata=xxx, output:x,goto(next table)

   default goto(next table)

* LOCAL_FLOOD
** todo: learning and/or l2 pop

   for each networks:
      // local_flood_update, local_flood_delete
      metadata=xxx, output:i,j,k
      or
      metadata=xxx,eth_dst=broadcast, output:i,j,k

   default drop

* references
** similar attempts for OVS agent https://wiki.openstack.org/wiki/Ovs-flow-logic
*** we use metadata instead of "internal" VLANs
*** we don't want to use NX learn action
"""

from neutron.plugins.common import constants as p_const
from neutron.plugins.ofagent.agent import ofswitch
from neutron.plugins.ofagent.agent import tables


class OFAgentIntegrationBridge(ofswitch.OpenFlowSwitch):
    """ofagent br-int specific logic."""

    def setup_default_table(self):
        self.delete_flows()

        self.install_default_drop(tables.CHECK_IN_PORT)

        for t in tables.TUNNEL_IN.values():
            self.install_default_drop(t)
        self.install_default_goto(tables.PHYS_IN, tables.TUNNEL_OUT)
        self.install_default_goto_next(tables.LOCAL_IN)

        self.install_default_goto_next(tables.TUNNEL_OUT)
        self.install_default_goto_next(tables.LOCAL_OUT)
        self.install_default_goto_next(tables.PHYS_OUT)

        for t in tables.TUNNEL_FLOOD.values():
            self.install_default_goto_next(t)
        self.install_default_goto_next(tables.PHYS_FLOOD)
        self.install_default_drop(tables.LOCAL_FLOOD)

    def install_tunnel_output(self, table_id,
                              metadata, segmentation_id,
                              ports, goto_next, **additional_matches):
        (dp, ofp, ofpp) = self._get_dp()
        match = ofpp.OFPMatch(metadata=metadata, **additional_matches)
        actions = [ofpp.OFPActionSetField(tunnel_id=segmentation_id)]
        actions += [ofpp.OFPActionOutput(port=p) for p in ports]
        instructions = [
            ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions),
        ]
        if goto_next:
            instructions += [
                ofpp.OFPInstructionGotoTable(table_id=table_id + 1),
            ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=table_id,
                              priority=1,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def delete_tunnel_output(self, table_id,
                             metadata, **additional_matches):
        (dp, _ofp, ofpp) = self._get_dp()
        self.delete_flows(table_id=table_id, metadata=metadata,
                          **additional_matches)

    def provision_tenant_tunnel(self, network_type, tenant, segmentation_id):
        (dp, _ofp, ofpp) = self._get_dp()
        match = ofpp.OFPMatch(tunnel_id=segmentation_id)
        instructions = [
            ofpp.OFPInstructionWriteMetadata(metadata=tenant,
                                             metadata_mask=0xffffffff),
            ofpp.OFPInstructionGotoTable(table_id=tables.PHYS_OUT),
        ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=tables.TUNNEL_IN[network_type],
                              priority=1,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def reclaim_tenant_tunnel(self, network_type, tenant, segmentation_id):
        table_id = tables.TUNNEL_IN[network_type]
        self.delete_flows(table_id=table_id, tunnel_id=segmentation_id)

    def provision_tenant_physnet(self, network_type, tenant,
                                 segmentation_id, phys_port):
        """for vlan and flat."""
        assert(network_type in [p_const.TYPE_VLAN, p_const.TYPE_FLAT])
        (dp, ofp, ofpp) = self._get_dp()

        instructions = [ofpp.OFPInstructionWriteMetadata(metadata=tenant,
                        metadata_mask=0xffffffff)]
        if network_type == p_const.TYPE_VLAN:
            vlan_vid = segmentation_id | ofp.OFPVID_PRESENT
            match = ofpp.OFPMatch(in_port=phys_port, vlan_vid=vlan_vid)
            actions = [ofpp.OFPActionPopVlan()]
            instructions += [ofpp.OFPInstructionActions(
                             ofp.OFPIT_APPLY_ACTIONS, actions)]
        else:
            match = ofpp.OFPMatch(in_port=phys_port)
        instructions += [ofpp.OFPInstructionGotoTable(table_id=tables.PHYS_IN)]
        msg = ofpp.OFPFlowMod(dp,
                              priority=1,
                              table_id=tables.CHECK_IN_PORT,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

        match = ofpp.OFPMatch(metadata=tenant)
        if network_type == p_const.TYPE_VLAN:
            actions = [
                ofpp.OFPActionPushVlan(),
                ofpp.OFPActionSetField(vlan_vid=vlan_vid),
            ]
        else:
            actions = []
        actions += [ofpp.OFPActionOutput(port=phys_port)]
        if network_type == p_const.TYPE_VLAN:
            actions += [ofpp.OFPActionPopVlan()]
        instructions = [
            ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions),
            ofpp.OFPInstructionGotoTable(table_id=tables.PHYS_FLOOD + 1),
        ]
        msg = ofpp.OFPFlowMod(dp,
                              priority=1,
                              table_id=tables.PHYS_FLOOD,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def reclaim_tenant_physnet(self, network_type, tenant,
                               segmentation_id, phys_port):
        (_dp, ofp, _ofpp) = self._get_dp()
        vlan_vid = segmentation_id | ofp.OFPVID_PRESENT
        if network_type == p_const.TYPE_VLAN:
            self.delete_flows(table_id=tables.CHECK_IN_PORT,
                              in_port=phys_port, vlan_vid=vlan_vid)
        else:
            self.delete_flows(table_id=tables.CHECK_IN_PORT,
                              in_port=phys_port)
        self.delete_flows(table_id=tables.PHYS_FLOOD, metadata=tenant)

    def check_in_port_add_tuunel_port(self, tunnel_type, port):
        (dp, _ofp, ofpp) = self._get_dp()
        match = ofpp.OFPMatch(in_port=port)
        instructions = [
            ofpp.OFPInstructionGotoTable(
                table_id=tables.TUNNEL_IN[tunnel_type])
        ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=tables.CHECK_IN_PORT,
                              priority=1,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def check_in_port_add_local_port(self, tenant, port):
        (dp, ofp, ofpp) = self._get_dp()
        match = ofpp.OFPMatch(in_port=port)
        instructions = [
            ofpp.OFPInstructionWriteMetadata(metadata=tenant,
                                             metadata_mask=0xffffffff),
            ofpp.OFPInstructionGotoTable(table_id=tables.LOCAL_IN),
        ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=tables.CHECK_IN_PORT,
                              priority=1,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def check_in_port_delete_port(self, port):
        self.delete_flows(table_id=tables.CHECK_IN_PORT, in_port=port)

    def local_flood_update(self, tenant, ports, flood_unicast):
        (dp, ofp, ofpp) = self._get_dp()
        match_all = ofpp.OFPMatch(metadata=tenant)
        match_multicast = ofpp.OFPMatch(metadata=tenant,
                                        eth_dst=('01:00:00:00:00:00',
                                                 '01:00:00:00:00:00'))
        if flood_unicast:
            match_add = match_all
            match_del = match_multicast
        else:
            match_add = match_multicast
            match_del = match_all
        actions = [ofpp.OFPActionOutput(port=p) for p in ports]
        instructions = [
            ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions),
        ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=tables.LOCAL_FLOOD,
                              priority=1,
                              match=match_add,
                              instructions=instructions)
        self._send_msg(msg)
        self.delete_flows(table_id=tables.LOCAL_FLOOD, strict=True,
                          priority=1, match=match_del)

    def local_flood_delete(self, tenant):
        self.delete_flows(table_id=tables.LOCAL_FLOOD, metadata=tenant)

    def local_out_add_port(self, tenant, port, mac):
        (dp, ofp, ofpp) = self._get_dp()
        match = ofpp.OFPMatch(metadata=tenant, eth_dst=mac)
        actions = [ofpp.OFPActionOutput(port=port)]
        instructions = [
            ofpp.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions),
        ]
        msg = ofpp.OFPFlowMod(dp,
                              table_id=tables.LOCAL_OUT,
                              priority=1,
                              match=match,
                              instructions=instructions)
        self._send_msg(msg)

    def local_out_delete_port(self, tenant, mac):
        self.delete_flows(table_id=tables.LOCAL_OUT,
                          metadata=tenant, eth_dst=mac)
