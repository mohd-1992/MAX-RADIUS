"""Classification refinement is one point, never another counter contribution."""
import datetime as dt
import unittest
from services.network_traffic_service import point_info
from services.traffic_point_resolution_service import aliases_from_points,merge_points,normalize_selection
from services.traffic_interval_service import filters


class PointResolution(unittest.TestCase):
    def point(self,kind='',nas='192.0.2.10',name='HS',interface='',**values):
        return dict(point_info(dict(nas_address=nas,service_type=kind,server_name=name,server_interface=interface),{}),**values)
    def test_one_hotspot_absorbs_untyped_predecessor_without_changing_inputs(self):
        old=self.point(download=100,upload=None,sessions=2,last_observed=dt.datetime(2026,11,10))
        new=self.point('hotspot',download=40,upload=0,sessions=1,last_observed=dt.datetime(2026,11,11))
        aliases=aliases_from_points([old,new]);result=merge_points([old,new],aliases)
        self.assertEqual(len(result),1);self.assertEqual((result[0]['download'],result[0]['upload'],result[0]['sessions']),(140,0,3))
        self.assertEqual(result[0]['service_type'],'hotspot');self.assertTrue(result[0]['point_known']);self.assertEqual(old['service_type'],'')
    def test_other_nas_or_name_never_joins(self):
        for new in (self.point('hotspot',nas='192.0.2.11'),self.point('hotspot',name='HS2')):
            old=self.point();self.assertNotIn(old['server_key'],aliases_from_points([old,new]))
    def test_conflicting_protocol_and_multiple_ppp_interfaces_stay_separate(self):
        old=self.point();hot=self.point('hotspot');ppp=self.point('pppoe',interface='bridge1')
        self.assertNotIn(old['server_key'],aliases_from_points([old,hot,ppp]))
        self.assertFalse(aliases_from_points([old,ppp,self.point('pppoe',interface='bridge2')]))
    def test_pppoe_alone_does_not_supply_a_missing_interface(self):
        self.assertFalse(aliases_from_points([self.point(),self.point('pppoe',interface='bridge1')]))
    def test_incomplete_ppp_evidence_still_blocks_hotspot_alias(self):
        self.assertFalse(aliases_from_points([self.point('hotspot'),self.point('pppoe')]))
    def test_old_url_and_known_filter_share_exact_same_keys(self):
        old=self.point();known=self.point('hotspot');aliases=aliases_from_points([known])
        for key in (old['server_key'],known['server_key']):
            clauses,args=filters(dict(server=key,service_type='hotspot'),7,aliases=aliases)
            self.assertIn(7,args);self.assertIn(old['server_key'],args);self.assertIn(known['server_key'],args)
            self.assertEqual(normalize_selection(dict(server=key),aliases)['server'],known['server_key'])
        clauses,args=filters(dict(service_type='unknown'),None,aliases=aliases);self.assertIn(old['server_key'],args);self.assertIn('NOT IN',' '.join(clauses))
    def test_invalid_known_identity_cannot_reclassify(self):
        fake=self.point('hotspot');fake['server_key']='a'*64
        self.assertFalse(aliases_from_points([fake]))


if __name__=='__main__':unittest.main()
