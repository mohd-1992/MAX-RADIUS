"""Pure recorded-point display checks. No application, DB or NAS imports."""
import unittest
from services.session_display_service import enrich_session_display,connection_point_label,index_live_sessions
from pathlib import Path
from jinja2 import Environment

class SessionPointLabels(unittest.TestCase):
    def test_live_count_combines_nas_and_case_without_extra_reads(self):
        rows=[dict(username='Account',nasipaddress='192.0.2.1'),
              dict(username='account',nasipaddress='192.0.2.2'),
              dict(username='single',nasipaddress='192.0.2.1')]
        representatives,counts=index_live_sessions(rows)
        self.assertEqual(counts,{'account':2,'single':1})
        self.assertIs(representatives['account'],rows[1])
        self.assertEqual(index_live_sessions(None),({ },{ }))

    def test_actual_list_badges_hide_offline_count(self):
        root=Path(__file__).resolve().parents[1]/'web/templates'
        for name,var in [('subscribers.html','s'),('active_card_users.html','c')]:
            source=(root/name).read_text(encoding='utf-8')
            Environment().parse(source)
            start=source.index('<span class="inline-flex items-center gap-1 shrink-0" title=')
            end=source.index('{% if can_access_endpoint(',start)
            template=Environment().from_string(source[start:end])
            for count in (0,1,2,3):
                html=template.render(**{var:dict(is_online=count>0,active_session_count=count)})
                self.assertEqual('data-active-session-count' in html,bool(count))
                if count:self.assertIn(f'data-active-session-count="{count}"',html)

    def test_missing_protocol_keeps_name_and_nas_catalog(self):
        rows=[dict(nasipaddress='192.0.2.10',calledstationid='HS-Branch',nasportid='ether2',servicetype='',framedprotocol='')]
        result=enrich_session_display(rows,lambda *a:[dict(address='192.0.2.10',label='Router')])
        self.assertEqual(result[0]['connection_point'],'HS-Branch')
        self.assertEqual(result[0]['nas_display_name'],'Router')
        self.assertEqual(result[0]['servicetype'],'')
    def test_known_kinds_keep_prefix_and_pppoe_interface(self):
        self.assertEqual(connection_point_label('HS','hotspot','ether1'),'Hotspot · HS')
        self.assertEqual(connection_point_label('P','pppoe','bridge1'),'PPPoE · P · bridge1')
        self.assertEqual(connection_point_label('P','pppoe','P'),'PPPoE · P')
    def test_empty_unsafe_and_conflicting_names_not_invented(self):
        for name,kind in [('', 'hotspot'),(' ',''),('bad'+chr(127),''),('HS','conflict')]:
            self.assertEqual(connection_point_label(name,kind),'غير محدد')
        self.assertEqual(connection_point_label('P','pppoe','bad'+chr(127)),'PPPoE · P')

if __name__=='__main__':unittest.main()
