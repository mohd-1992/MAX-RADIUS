"""Pure historical-source resolution; no app, database, Docker or NAS."""
import unittest
from services.sales_source_service import resolve_historical_source, server_key

class HistorySourceContracts(unittest.TestCase):
    def row(self, **kw):
        return dict(dict(nasipaddress='192.0.2.1',calledstationid='HS1',nasportid='ether1',
                         acctuniqueid='uid1',acctterminatecause='',framedprotocol='',servicetype='Login-User'),**kw)
    def test_packet_proves_service_and_point(self):
        r=resolve_historical_source({},[self.row()])
        self.assertEqual((r['service_type'],r['service_attribution'],r['resolution_status']),('hotspot','packet_protocol','confirmed'))
    def test_package_reference_is_explicitly_inferred(self):
        r=resolve_historical_source({},[self.row(servicetype='')],'hotspot')
        self.assertEqual((r['resolution_status'],r['service_attribution']),('inferred','package_reference'))
        self.assertNotEqual(r['server_key'],'unknown')
    def test_no_session_never_invents_a_point(self):
        r=resolve_historical_source({},[],'pppoe')
        self.assertEqual((r['service_type'],r['server_key'],r['resolution_status']),('pppoe','unknown','inferred'))
    def test_service_survives_missing_server(self):
        r=resolve_historical_source({},[self.row(calledstationid='')])
        self.assertEqual((r['service_type'],r['nas_address'],r['server_key']),('hotspot','192.0.2.1','unknown'))
    def test_distinct_internal_servers_are_ambiguous(self):
        r=resolve_historical_source({},[self.row(),self.row(calledstationid='HS2')])
        self.assertEqual((r['resolution_status'],r['server_key']),('ambiguous','unknown'))
    def test_hotspot_ports_do_not_split_a_server(self):
        r=resolve_historical_source({},[self.row(),self.row(nasportid='ether2')])
        self.assertEqual(r['resolution_status'],'confirmed')
    def test_pppoe_interfaces_are_independent(self):
        r=resolve_historical_source({},[self.row(framedprotocol='PPP',servicetype='',nasportid='bridge1'),self.row(framedprotocol='PPP',servicetype='',nasportid='bridge2')])
        self.assertEqual(r['resolution_status'],'ambiguous')
    def test_conflicting_protocol_is_not_guessed(self):
        r=resolve_historical_source({},[self.row(framedprotocol='PPP')],'hotspot')
        self.assertEqual((r['resolution_status'],r['server_key']),('ambiguous','unknown'))
    def test_confirmed_snapshot_is_immutable(self):
        old=dict(server_key=server_key('192.0.2.1','hotspot','old'),service_type='hotspot',server_name='old',nas_address='192.0.2.1',attribution='accounting_start')
        r=resolve_historical_source(old,[self.row(calledstationid='new')],'pppoe')
        self.assertEqual((r['server_key'],r['server_name'],r['service_type']),(old['server_key'],'old','hotspot'))
    def test_loopback_and_consolidated_are_not_proof(self):
        for row in (self.row(nasipaddress='127.0.0.1'),self.row(acctterminatecause='Consolidated-Historical-Import')):
            self.assertEqual(resolve_historical_source({},[row])['server_key'],'unknown')
    def test_copied_sessions_keep_stable_evidence(self):
        a,b=self.row(acctuniqueid='b'),self.row(acctuniqueid='a')
        self.assertEqual(resolve_historical_source({},[a,b]),resolve_historical_source({},[b,a]))
    def test_package_edit_does_not_rewrite_stored_inference(self):
        old=resolve_historical_source({},[self.row(servicetype='')],'hotspot')
        r=resolve_historical_source(old,[self.row(servicetype='')],'pppoe')
        self.assertEqual((r['service_type'],r['server_key']),(old['service_type'],old['server_key']))
    def test_removed_session_does_not_erase_stored_point(self):
        old=resolve_historical_source({},[self.row(servicetype='')],'hotspot')
        r=resolve_historical_source(old,[],'hotspot')
        self.assertEqual(r['server_key'],old['server_key'])
        self.assertEqual(r['resolution_reason'],'evidence_no_longer_available')
    def test_control_character_does_not_form_a_server(self):
        r=resolve_historical_source({},[self.row(calledstationid='bad'+chr(127))])
        self.assertEqual(r['server_key'],'unknown')
    def test_current_package_does_not_override_packet_proof(self):
        r=resolve_historical_source({},[self.row()],'pppoe')
        self.assertEqual((r['service_type'],r['resolution_status']),('hotspot','confirmed'))

if __name__=='__main__':unittest.main()
