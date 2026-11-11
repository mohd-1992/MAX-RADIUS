"""Share applicable report filters without carrying pagination between views."""

COMMON = ('period','date_from','date_to','nas','service_type','server','basis')


def navigation(args):
    common={key:str(args.get(key) or '') for key in COMMON if args.get(key)}
    for key in ('nas','service_type','server'):
        if common.get(key)=='all':common.pop(key)
    ledger={key:str(value) for key,value in args.items()
            if key not in ('page','record_kind','report_view') and value is not None}
    return dict(report_navigation=common,ledger_navigation=ledger)
