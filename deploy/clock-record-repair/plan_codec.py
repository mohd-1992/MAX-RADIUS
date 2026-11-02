"""Versioned JSON round-trip; no pickle/eval and no connection credentials."""
import datetime
import decimal
import json


def encode(value):
    if isinstance(value,datetime.datetime):return {'$clock_type':'datetime','value':value.isoformat()}
    if isinstance(value,datetime.date):return {'$clock_type':'date','value':value.isoformat()}
    if isinstance(value,decimal.Decimal):return {'$clock_type':'decimal','value':str(value)}
    if isinstance(value,bytes):return {'$clock_type':'bytes','value':value.hex()}
    if isinstance(value,dict):return {k:encode(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [encode(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise TypeError('Unsupported plan value')


def decode(value):
    if isinstance(value,list):return [decode(v) for v in value]
    if isinstance(value,dict):
        if '$clock_type' in value:
            if set(value)!={'$clock_type','value'}:raise ValueError('Invalid typed value')
            kind=value['$clock_type'];text=value['value']
            if kind=='datetime':return datetime.datetime.fromisoformat(text)
            if kind=='date':return datetime.date.fromisoformat(text)
            if kind=='decimal':return decimal.Decimal(text)
            if kind=='bytes':return bytes.fromhex(text)
            raise ValueError('Unknown typed value')
        return {k:decode(v) for k,v in value.items()}
    return value


def dumps(plan):return json.dumps({'format':'max-clock-plan-v1','plan':encode(plan)},sort_keys=True,separators=(',',':'))


def loads(text):
    value=json.loads(text)
    if set(value)!={'format','plan'} or value['format']!='max-clock-plan-v1':raise ValueError('Invalid plan envelope')
    return decode(value['plan'])
