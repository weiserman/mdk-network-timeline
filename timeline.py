"""Build the network-timeline data from a Mobile Services HAR trace and (optionally) an MDK client log.

In short:

- Mobile Services logs each OData call once per hop (app proxy -> destination -> Cloud Connector/SAP).
  The hops of one call are folded: OuterMs = first hop, InnerMs = the /opu/odata4/ hop,
  SAP backend = sap-perf-fesrec, Mobile Services = Outer - Inner, Cloud Connector + network = Inner - SAP.
- Offline-sync windows cluster the MobiLink / sync-progress calls and the OData reads the offline
  server makes during a download; time inside a window when no call is running is device / network.
- The client log is placed on the HAR clock using the "SUPPORT LOG SENT" marker vs the log upload POST.
- Optionally one online "action" call (e.g. a POST that creates something) and the offline download that
  follows it are broken down step by step. Both are auto-detected unless a URL pattern is given.
"""
import argparse
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

TEMPLATE = Path(__file__).with_name('timeline_template.html')


def _ts(s):
    """ISO timestamp -> aware UTC datetime."""
    d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ms(delta):
    return round(delta.total_seconds() * 1000)


def _resource(url):
    """Path after the service root, so the hops of one call compare equal."""
    u = urlsplit(url)
    p = unquote(u.path + ('?' + u.query if u.query else ''))
    m = re.search(r'/opu/odata4/.+?/\d{4}(/.*)?$', p)
    if m:
        return m.group(1) or '/'
    return re.sub(r'^/[^/]+(?=/|$)', '', p) or '/'


# ---- HAR ------------------------------------------------------------------------------------------
def fold_har(har):
    """Return the HAR calls with their hops folded, sorted by start."""
    entries = sorted(har.get('log', {}).get('entries', []), key=lambda e: _ts(e['startedDateTime']))
    if not entries:
        raise ValueError('The HAR file has no entries.')
    hops = []
    for e in entries:
        start = _ts(e['startedDateTime'])
        hdr = {h['name'].lower(): h['value'] for h in e.get('response', {}).get('headers', [])}
        req = {h['name'].lower(): h['value'] for h in e.get('request', {}).get('headers', [])}
        fes = hdr.get('sap-perf-fesrec')
        transid, prev = _passport(req.get('sap-passport'))
        hops.append({
            'start': start, 'end': start + timedelta(milliseconds=e.get('time') or 0),
            'method': e['request']['method'], 'status': e['response'].get('status'),
            'time': round(e.get('time') or 0),
            'backend': round(float(fes) / 1000) if fes else None,
            'inner': '/opu/odata4/' in e['request']['url'],
            'bytes': (e['response'].get('content') or {}).get('size'),
            'resource': _resource(e['request']['url']),
            # tracing context, kept for the end-to-end view
            'req': req, 'res': hdr, 'entry': e, 'transid': transid, 'prev': prev,
        })

    groups = []
    for h in hops:
        key = f"{h['method']} {h['resource']}"
        g = next((g for g in reversed(groups)
                  if g['key'] == key and h['start'] <= g['outer']['end'] and not g['inner']), None)
        if g:
            g['hops'].append(h)
            if h['inner']:
                g['inner'] = h
        else:
            groups.append({'key': key, 'outer': h, 'inner': h if h['inner'] else None, 'hops': [h]})

    calls = []
    for g in groups:
        o, i = g['outer'], g['inner']
        be = next((h['backend'] for h in g['hops'] if h['backend'] is not None), None)
        calls.append({
            'start_utc': o['start'], 'method': o['method'], 'status': o['status'], 'hops': len(g['hops']),
            'outer': o['time'], 'inner': i['time'] if i else None, 'backend': be,
            'mobsvc': o['time'] - i['time'] if i and len(g['hops']) > 1 else None,
            'ccnet': i['time'] - be if i and be is not None else None,
            'bytes': o['bytes'], 'url': o['resource'],
            'req': o['req'], 'res': o['res'], 'transid': o['transid'],
            'prevs': [h['prev'] for h in g['hops'] if h['prev']],
            'corr': o['req'].get('x-correlationid'), 'trace_type': o['req'].get('networktracetype', ''),
            'inner_entry': (i or o)['entry'], 'inner_req': (i or o)['req'], 'inner_res': (i or o)['res'],
        })
    return sorted(calls, key=lambda c: c['start_utc'])


def _passport(hexstr):
    """SAP Passport (hex) -> (transaction ID, previous component). The transaction ID is the same on
    every hop of one request chain - device, Mobile Services, Cloud Connector and the ABAP server - so
    it is the key to find this request in SAP (STAD / SAT / ST05)."""
    if not hexstr or not re.fullmatch(r'[0-9A-Fa-f]+', hexstr) or len(hexstr) % 2:
        return None, None
    raw = bytes.fromhex(hexstr)
    if not raw.startswith(b'*TH*'):
        return None, None
    text = raw.decode('latin-1')
    m = re.search(r'[0-9A-F]{32}', text)
    if not m:
        return None, None
    prev = text[max(0, m.start() - 32):m.start()].replace('\x00', '').strip() or None
    return m.group(0), prev


def _kind(c):
    if re.search(r'ServletAsync|MobiLink|syncprogress', c['url']):
        return 'sync'          # offline sync protocol
    if c['backend'] is not None and c['hops'] >= 3:
        return 'online'        # device -> app proxy -> SAP
    if c['backend'] is not None:
        return 'offline'       # issued by the offline server during a download
    return 'platform'          # settings, lcm, client logs ...


# ---- Client log -----------------------------------------------------------------------------------
def read_client_log(doc):
    """Deduplicated client-log lines (MDK writes most lines twice: ROOT and SAP.MDKClient)."""
    raw = doc.get('clientLogs') if isinstance(doc, dict) else None
    if not raw:
        raise ValueError('The client log has no "clientLogs" entries.')
    lines = sorted(({'line': int(x.get('lineNumber') or 0), 'ts': _ts(x['timestamp']), 'sev': int(x.get('severity') or 0),
                     'src': x.get('sourceName') or '', 'msg': str(x.get('messageText') or '')} for x in raw),
                   key=lambda l: (l['ts'], l['line']))
    seen, out = set(), []
    for l in lines:
        k = re.sub(r'^- TRACE - ', '', l['msg']).strip() + '|' + l['ts'].strftime('%H:%M:%S.') + l['ts'].strftime('%f')[0]
        if l['src'] == 'SAP.MDKClient' and k in seen:
            continue
        seen.add(k)
        out.append(l)
    device = next((l['msg'] for l in out if l['msg'].startswith('Device[')), '')
    return {'lines': out, 'user': raw[0].get('username') or '', 'device': device}


def _device_type(sev, m):
    if 'SUPPORT LOG SENT' in m:
        return 'app'
    if sev >= 5:
        return 'error'
    if sev == 4:
        return 'warn'
    if re.match(r'^\[[^\]]+\]', m):   # app log lines written as "[Tag] message"
        return 'app'
    if re.match(r'^OData \w+ - \d+ - \d+ - \d+ ms', m):
        return 'read'
    if re.match(r'^(download|upload)\b.*(succeeded|failed)', m):
        return 'sync'
    if re.search(r'onSuspend|EnterBackground|EnterForeground|onAppResumed|User logout|onUserSwitched|logging service policy', m):
        return 'life'
    return None


# ---- Timeline data --------------------------------------------------------------------------------
def _name(url):
    """Short name of a call: last path segment without query or namespace (SAP__self.createOrder -> createOrder)."""
    seg = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1] or '/'
    return seg.rsplit('.', 1)[-1] if '.' in seg and not seg.startswith('$') else seg


def _match(pattern, url):
    """A pattern matches a URL as a case-insensitive regular expression (a plain substring works too)."""
    try:
        return re.search(pattern, url, re.I) is not None
    except re.error:
        return pattern.lower() in url.lower()


def _pick_action(rows, sync, pattern):
    """The online call whose offline download is broken down: the first match for `pattern`, or, without
    one, the first online call that changes data (not GET / HEAD) and is followed by a sync within 15 s."""
    for r in rows:
        if r['kind'] != 'online':
            continue
        if pattern:
            if _match(pattern, r['url']):
                return r
        elif r['method'] not in ('GET', 'HEAD') and any(
                0 <= w['start'] - (r['start'] + r['dur']) < 15000 for w in sync):
            return r
    return None


def build(har, client_log=None, title='MDK app - network timeline', har_name='', log_name='',
          device_offset_ms=None, sync_gap_ms=4000, idle_gap_ms=400, action=None, download=None):
    """`action` / `download`: optional URL patterns for the online call to break down and the offline
    read(s) to follow end to end; both are auto-detected when left empty."""
    calls = fold_har(har)
    t0 = calls[0]['start_utc']
    rows = [{'start': _ms(c['start_utc'] - t0), 'dur': c['outer'], 'kind': _kind(c), 'method': c['method'],
             'status': c['status'], 'hops': c['hops'], 'inner': c['inner'], 'sap': c['backend'], 'cc': c['ccnet'],
             'ms': c['mobsvc'], 'bytes': c['bytes'], 'url': c['url']} for c in calls]
    span = max(r['start'] + r['dur'] for r in rows) or 1

    # Offline sync windows
    windows = []
    for r in (r for r in rows if r['kind'] in ('sync', 'offline')):
        w = windows[-1] if windows else None
        if w and r['start'] <= w['end'] + sync_gap_ms:
            w['calls'].append(r)
            w['end'] = max(w['end'], r['start'] + r['dur'])
        else:
            windows.append({'start': r['start'], 'end': r['start'] + r['dur'], 'calls': [r]})
    sync = []
    for n, w in enumerate(windows, 1):
        busy, gaps, cs, ce = 0, [], None, None
        for c in sorted(w['calls'], key=lambda c: c['start']):
            a, b = c['start'], c['start'] + c['dur']
            if cs is None:
                cs, ce = a, b
            elif a > ce:
                busy += ce - cs
                if a - ce >= idle_gap_ms:
                    gaps.append({'from': ce, 'to': a})
                cs, ce = a, b
            else:
                ce = max(ce, b)
        busy += ce - cs
        dur = w['end'] - w['start']
        sync.append({'n': n, 'start': w['start'], 'end': w['end'], 'dur': dur, 'busy': busy, 'idle': dur - busy,
                     'sap': sum(c['sap'] for c in w['calls'] if c['sap'] is not None), 'calls': len(w['calls']),
                     'gaps': gaps, 'reads': ', '.join(c['url'].split('?')[0] for c in w['calls'] if c['kind'] == 'offline')})

    # Client log
    notes, device, device_info, offset, offset_how = [], [], '', 0, ''
    device_calls = []   # durations the device itself measured: online calls + app-timed steps
    if client_log:
        cl = read_client_log(client_log)
        device_info = f"{cl['device']} / {cl['user']}".strip(' /')
        if device_offset_ms is not None:
            offset, offset_how = device_offset_ms, 'set manually'
        else:
            marker = next((l for l in reversed(cl['lines']) if 'SUPPORT LOG SENT' in l['msg']), None)
            upload = next((c for c in reversed(calls) if 'clientlogs' in c['url']), None)
            if marker and upload:
                offset = _ms(upload['start_utc'] - marker['ts'])
                offset_how = 'support-log marker (device) vs client-log upload (Mobile Services); includes upload latency'
            else:
                offset_how = 'not aligned (no support-log marker + upload pair found) - device times shown as logged'
        lo, hi = t0 - timedelta(seconds=2), t0 + timedelta(milliseconds=span + 2000)
        last_token = None
        for l in cl['lines']:
            ts = l['ts'] + timedelta(milliseconds=offset)
            m = re.sub(r'\s+', ' ', re.sub(r'^- TRACE - ', '', l['msg']))
            # The SDK reads its token from the secure store just before each HTTP request -> request start.
            if 'SecureKeyValueStore' in l['src']:
                last_token = l['ts']
                continue
            hit = re.match(r'^\[([^\]]+)\]\s*([\w ]+?)\s+status=(\d{3})', m)
            if hit and last_token and lo <= ts <= hi:
                device_calls.append({'at': _ms(ts - t0), 'name': f"{hit.group(1)}: {hit.group(2)}",
                                     'ms': _ms(l['ts'] - last_token), 'approx': True})
                last_token = None
            hit = re.match(r'^\[([^\]]+)\]\s*(\w+).*after (\d+) ms', m)
            if hit and lo <= ts <= hi:
                device_calls.append({'at': _ms(ts - t0), 'name': f"{hit.group(1)}: {hit.group(2)}",
                                     'ms': int(hit.group(3)), 'approx': False})
            if not lo <= ts <= hi:
                continue
            typ = _device_type(l['sev'], m)
            if not typ:
                continue
            device.append({'at': _ms(ts - t0), 'type': typ, 'sev': l['sev'],
                           'src': l['src'].replace('com.sap.cloud.mobile.', ''),
                           'msg': m if len(m) <= 220 else m[:220] + '...', 'dev': l['ts'].strftime('%H:%M:%S.%f')[:12]})
            hit = re.search(r'^\[([^\]]+)\].*after (\d+) ms', m)
            if hit:
                notes.append(f"Device measured ({hit.group(1)}) at {_ms(ts - t0) / 1000:.1f} s: {m}")
        if not any(d['type'] in ('app', 'read', 'sync') and 'SUPPORT LOG SENT' not in d['msg'] for d in device):
            notes.append('The client log has no app or trace lines inside the HAR window, so device-side durations '
                         'cannot be measured for this run. Set the client log level to Debug before the test '
                         '(a re-logon re-applies the Mobile Services log policy).')

    # Headline numbers and notes
    odata = [r for r in rows if r['sap'] is not None]
    tot = lambda rs, k: int(sum(r[k] for r in rs if r[k] is not None))
    stats = {'calls': len(rows), 'odataCalls': len(odata), 'span': span, 'network': tot(rows, 'dur'),
             'sap': tot(odata, 'sap'), 'cc': tot(odata, 'cc'), 'mobsvc': tot(odata, 'ms'),
             'other': tot([r for r in rows if r['sap'] is None], 'dur')}
    notes.insert(0, f"{len(odata)} SAP OData calls: SAP backend {stats['sap']} ms, Cloud Connector + network {stats['cc']} ms, "
                    f"Mobile Services {stats['mobsvc']} ms. The other {len(rows) - len(odata)} calls (sync protocol, "
                    f"platform) add {stats['other']} ms.")
    for w in sync:
        notes.append(f"Offline sync #{w['n']} at {w['start'] / 1000:.1f} s: {w['dur'] / 1000:.1f} s wall clock, Mobile Services "
                     f"busy {w['busy'] / 1000:.1f} s, idle {w['idle'] / 1000:.1f} s ({round(100 * w['idle'] / max(1, w['dur']))}%) "
                     f"waiting on the device / network; SAP {w['sap']} ms ({w['reads']}).")
    milestones, breakdown = [], None
    reg = _pick_action(rows, sync, action)
    act = _name(reg['url']) if reg else None
    if reg:
        after = next((w for w in sync if w['start'] >= reg['start']), None)
        if after:
            breakdown = _breakdown(rows, reg, after, device_calls, act)
            milestones.append({'from': breakdown['from'], 'to': breakdown['to'], 'label': f'{act} -> downloaded'})
            notes.append(breakdown['summary'])
        how = 'matched by the action pattern' if action else 'auto-detected: first online change followed by a sync'
        notes.append(f"Action call: {reg['method']} {reg['url'].split('?')[0]} ({how}).")
    elif action:
        notes.append(f'No online call matches the action pattern "{action}".')
    e2e = _e2e(calls, rows, reg, download)
    if client_log:
        notes.append(f"Device clock offset {offset} ms ({offset_how}).")

    return {'title': title, 'har': har_name, 'clientLog': log_name, 't0': t0.isoformat().replace('+00:00', 'Z'),
            'action': act,
            'deviceInfo': device_info, 'stats': stats, 'rows': rows, 'sync': sync, 'device': device,
            'milestones': milestones, 'breakdown': breakdown, 'e2e': e2e, 'notes': notes}


# ---- End to end: one offline download -------------------------------------------------------------
def _payload(entry, res, req):
    """Size and shape of an OData JSON response body (the HAR from Mobile Services keeps the body)."""
    c = (entry.get('response') or {}).get('content') or {}
    text = c.get('text')
    if text and c.get('encoding') == 'base64':
        text = base64.b64decode(text).decode('utf-8', 'replace')
    info = {'jsonBytes': len(text.encode('utf-8')) if text else None,
            'wireBytes': int(res.get('content-length') or c.get('size') or 0) or None,
            'encoding': res.get('content-encoding'), 'rows': None, 'nav': {}, 'deltaLink': None,
            'prefer': req.get('prefer'), 'preferenceApplied': res.get('preference-applied')}
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        vals = body.get('value') if isinstance(body.get('value'), list) else [body]
        info['rows'] = len(vals)
        for v in vals:
            for k, x in (v.items() if isinstance(v, dict) else []):
                if isinstance(x, list):
                    info['nav'][k] = info['nav'].get(k, 0) + len(x)
        info['deltaLink'] = bool(body.get('@odata.deltaLink'))
    return info


def _e2e(calls, rows, reg, pattern=None):
    """Follow offline-server reads end to end: every read matching `pattern`, or, without one, the
    slowest entity read (not $metadata or the service document) of each sync session.

    Links used (all from the HAR):
    - the backend read carries X-Correlationid = the correlation ID of the device's MobiLink request
      that started the download;
    - that MobiLink request carries operationid = the sync operation; every ServletAsync request of the
      session and the SDK's SyncStatusSet('<operationid>') polls carry it too;
    - every hop carries the same SAP Passport transaction ID, which the ABAP server records as well.
    """
    out = []
    reads = [i for i, c in enumerate(calls) if rows[i]['kind'] == 'offline']
    if pattern:
        targets = [i for i in reads if _match(pattern, calls[i]['url'])]
    else:
        by_session = {}
        for i in reads:
            if calls[i]['url'] == '/' or calls[i]['url'].startswith('/$metadata'):
                continue
            key = calls[i]['corr'] or i
            if key not in by_session or rows[i]['dur'] > rows[by_session[key]]['dur']:
                by_session[key] = i
        targets = sorted(by_session.values())
    for i in targets:
        c, r = calls[i], rows[i]
        corr = c['corr']
        trig = next((j for j, x in enumerate(calls) if corr and x['corr'] == corr and 'ServletAsync' in x['url']
                     and x['trace_type'] == 'Client2Cpms'), None)
        op = calls[trig]['req'].get('operationid') if trig is not None else None
        member = {i}
        member |= {j for j, x in enumerate(calls) if corr and x['corr'] == corr and rows[j]['kind'] == 'offline'}
        if op:
            member |= {j for j, x in enumerate(calls)
                       if ('ServletAsync' in x['url'] and x['req'].get('operationid') == op) or f"('{op}')" in x['url']}
        s0 = min(rows[j]['start'] for j in member)
        s1 = max(rows[j]['start'] + rows[j]['dur'] for j in member)
        anchor = None
        if reg and reg['start'] + reg['dur'] <= s0 and s0 - (reg['start'] + reg['dur']) < 15000:
            anchor = reg['start'] + reg['dur']
            member |= {j for j, x in enumerate(calls) if anchor <= rows[j]['start'] < s0
                       and 'syncprogress' in x['url'] and x['trace_type'] == 'Client2Cpms'}
        start = anchor if anchor is not None else min(rows[j]['start'] for j in member)

        steps, dev_end, gaps = [], start, 0
        if anchor is not None:
            steps.append({'at': anchor, 'dur': 0, 'lane': 'marker', 'label': 'Action reply leaves Mobile Services',
                          'detail': 'Start of the wait for the data to reach the device', 'gap': None})
        for j in sorted(member, key=lambda j: rows[j]['start']):
            x, rr = calls[j], rows[j]
            device = x['trace_type'] == 'Client2Cpms' or rr['kind'] == 'sync'
            if device:
                gap = max(0, rr['start'] - dev_end)
                gaps += gap
                dev_end = max(dev_end, rr['start'] + rr['dur'])
                q = x['req']
                if 'ServletAsync' in x['url']:
                    no = q.get('ml-request-no', '?')
                    label = f"Device -> MobiLink request {no} ({x['method']})"
                    bits = []
                    if q.get('content-length'):
                        bits.append(f"sends {q['content-length']} bytes")
                    if q.get('ml-get-bytes'):
                        bits.append(f"ml-get-bytes {q['ml-get-bytes']}")
                    if q.get('ml-post-bytes'):
                        bits.append(f"ml-post-bytes {q['ml-post-bytes']}")
                    if j == trig:
                        bits.insert(0, 'its correlation ID is on the backend reads below - this request triggers them')
                    detail = '; '.join(bits) or 'MobiLink protocol step'
                elif 'SyncStatusSet' in x['url']:
                    label, detail = 'Device -> sync-progress poll', f"SyncStatusSet for operation {op} (HTTP {x['status']})"
                else:
                    label, detail = 'Device -> sync-progress service $metadata', 'SDK progress reporting starts'
                steps.append({'at': rr['start'], 'dur': rr['dur'], 'lane': 'device', 'label': label, 'detail': detail,
                              'gap': gap, 'status': x['status']})
            else:
                parts = [f"SAP {rr['sap']} ms" if rr['sap'] is not None else None,
                         f"Cloud Connector + network {rr['cc']} ms" if rr['cc'] is not None else None,
                         f"Mobile Services {rr['ms']} ms" if rr['ms'] is not None else None]
                detail = ', '.join(p for p in parts if p)
                if j == i:
                    detail = 'THE FOLLOWED READ - ' + detail
                steps.append({'at': rr['start'], 'dur': rr['dur'], 'lane': 'server', 'label': f"Offline server -> SAP: GET {x['url']}",
                              'detail': detail, 'gap': None, 'sap': rr['sap'], 'cc': rr['cc'], 'ms': rr['ms'],
                              'inner': rr['inner'], 'status': x['status'], 'target': j == i})

        server = [calls[j] for j in member if rows[j]['kind'] == 'offline']
        dev_steps = [s for s in steps if s['lane'] == 'device']
        chain = []
        for p in ([calls[trig]['prevs'][0]] if trig is not None and calls[trig]['prevs'] else []) + c['prevs']:
            if p not in chain:
                chain.append(p)
        u = urlsplit(c['inner_entry']['request']['url'])
        out.append({
            'from': start, 'to': s1, 'total': s1 - start, 'sessionMs': s1 - s0, 'afterAction': anchor is not None,
            'url': c['url'], 'entity': _name(c['url']), 'backendPath': unquote(u.path + ('?' + u.query if u.query else '')),
            'startUtc': c['start_utc'].isoformat().replace('+00:00', 'Z'),
            'corr': corr, 'operationId': op, 'mlSession': calls[trig]['req'].get('ml-session-id-from-client') if trig is not None else None,
            'transId': c['transid'], 'passportChain': chain,
            'endUser': c['inner_req'].get('x-smp-endusername'), 'sccLocation': c['inner_req'].get('sap-connectivity-scc-location_id'),
            'fesrec': c['backend'], 'call': {k: r[k] for k in ('start', 'dur', 'sap', 'cc', 'ms', 'inner')},
            'payload': _payload(c['inner_entry'], c['inner_res'], c['inner_req']),
            'deviceRequests': len(dev_steps), 'deviceRequestMs': sum(s['dur'] for s in dev_steps), 'deviceGapMs': gaps,
            'serverCalls': len(server), 'serverMs': sum(rows[j]['dur'] for j in member if rows[j]['kind'] == 'offline'),
            'serverSap': sum(rows[j]['sap'] or 0 for j in member if rows[j]['kind'] == 'offline'),
            'steps': steps,
        })
    # the download that follows the action call first
    return sorted(out, key=lambda e: (not e['afterAction'], -e['from']))


# ---- Action -> data downloaded: where the time goes -----------------------------------------------
_PRI = {'sap': 4, 'cc': 3, 'ms': 2, 'proto': 1}


def _breakdown(rows, reg, after, device_calls, act):
    """Split [CSRF fetch (or the action call) .. end of the following offline sync] into what was happening.

    Every millisecond is given to the most specific activity running at that moment at Mobile Services:
    SAP backend > Cloud Connector + network > Mobile Services (OData) > sync protocol call > nothing.
    "Nothing" is time when no request is at Mobile Services - the device is working (app rules,
    offline store) or a request / response is travelling between the device and BTP; a HAR cannot
    tell those two apart. Inside one OData call the hops are assumed nested symmetrically
    (Mobile Services -> CC -> SAP -> CC -> Mobile Services); the per-call totals are exact.
    """
    csrf = next((r for r in reversed(rows) if r['kind'] == 'online' and r['method'] == 'HEAD'
                 and r['start'] < reg['start'] and reg['start'] - (r['start'] + r['dur']) < 3000), None)
    a, b = (csrf or reg)['start'], after['end']
    cat = ['idle'] * max(1, b - a)

    def paint(s, e, k):
        for t in range(max(int(s), a), min(int(e), b)):
            cur = cat[t - a]
            if cur == 'idle' or _PRI[k] > _PRI[cur]:
                cat[t - a] = k

    for r in rows:
        s, e = r['start'], r['start'] + r['dur']
        if e <= a or s >= b:
            continue
        if r['sap'] is None:
            paint(s, e, 'proto')
            continue
        inner = r['inner'] if r['inner'] is not None else r['dur'] - (r['ms'] or 0)
        ms_over, cc = max(0, r['dur'] - inner), max(0, inner - r['sap'])
        p1 = s + ms_over // 2
        p2 = p1 + cc // 2
        p3 = p2 + r['sap']
        p4 = p3 + (cc - cc // 2)
        for x, y, k in ((s, p1, 'ms'), (p1, p2, 'cc'), (p2, p3, 'sap'), (p3, p4, 'cc'), (p4, e, 'ms')):
            paint(x, y, k)

    # Idle time is attributed to the phase it falls in.
    reg_end, sync_start = reg['start'] + reg['dur'], after['start']
    totals = {k: 0 for k in ('sap', 'cc', 'ms', 'proto', 'idle_csrf', 'idle_pre', 'idle_sync')}
    runs, run = [], 0           # idle stretches inside the sync = device round trips
    for i, k in enumerate(cat):
        t = a + i
        if k == 'idle':
            k = 'idle_csrf' if t < reg['start'] else ('idle_pre' if t < sync_start else 'idle_sync')
        totals[k] += 1
        if k == 'idle_sync':
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    trips = [r for r in runs if r >= 30]

    def parts(r):
        if r['sap'] is None:
            return 'Mobile Services only (no SAP)'
        bits = [f"SAP {r['sap']} ms"]
        if r['cc'] is not None:
            bits.append(f"Cloud Connector + network {r['cc']} ms")
        if r['ms'] is not None:
            bits.append(f"Mobile Services {r['ms']} ms")
        return ', '.join(bits)

    steps = []
    if csrf:
        steps.append({'step': 'CSRF token fetch (HEAD /)', 'start': csrf['start'], 'ms': csrf['dur'], 'kind': 'call',
                      'detail': parts(csrf)})
        steps.append({'step': f'Device + network: CSRF reply to {act} request arriving', 'start': csrf['start'] + csrf['dur'],
                      'ms': reg['start'] - csrf['start'] - csrf['dur'], 'kind': 'idle',
                      'detail': f'App builds the {act} request; reply and request travel device <-> BTP'})
    steps.append({'step': f"Action call ({reg['method']} {act})", 'start': reg['start'], 'ms': reg['dur'], 'kind': 'call',
                  'detail': parts(reg)})
    steps.append({'step': f'Device + network: {act} reply to first sync request', 'start': reg_end,
                  'ms': sync_start - reg_end, 'kind': 'idle',
                  'detail': 'Reply travels to the device, app rules run, the offline store starts the download'})
    inside = [r for r in rows if after['start'] <= r['start'] <= after['end']]
    steps.append({'step': f"Offline sync (download of {', '.join(sorted({r['url'].split('?')[0] for r in inside if r['kind'] == 'offline'})) or 'defining requests'})",
                  'start': sync_start, 'ms': after['dur'], 'kind': 'sync',
                  'detail': (f"{len(inside)} calls at Mobile Services, {len(trips)} waits on the device of 30 ms or more "
                             f"(average {round(sum(trips) / len(trips)) if trips else 0} ms, longest {max(trips) if trips else 0} ms); "
                             f"SAP {sum(r['sap'] for r in inside if r['sap'] is not None)} ms")})

    total = b - a
    idle = totals['idle_csrf'] + totals['idle_pre'] + totals['idle_sync']
    summary = (f"{act} to data downloaded: {total / 1000:.2f} s. SAP {totals['sap']} ms, Cloud Connector + network "
               f"{totals['cc']} ms, Mobile Services {totals['ms'] + totals['proto']} ms, and {idle / 1000:.2f} s "
               f"({round(100 * idle / max(1, total))}%) with no request at Mobile Services (device working or "
               f"device <-> BTP network), of which {totals['idle_sync'] / 1000:.2f} s is between the {len(trips)} sync round trips.")

    # Device view (client log at Debug): the same steps as measured on the device.
    dev = []
    for d in device_calls:
        if a - 3000 <= d['at'] <= b + 3000:
            server = None
            if 'csrf' in d['name'].lower() and csrf:
                server = csrf['dur']
            elif act.lower() in d['name'].lower() and 'download' not in d['name'].lower():
                server = reg['dur']
            elif 'download' in d['name'].lower():
                server = b - reg_end
            dev.append({**d, 'server': server})

    strip = []   # the same split in time order, as runs of one component
    for i, k in enumerate(cat):
        if strip and strip[-1]['k'] == k:
            strip[-1]['ms'] += 1
        else:
            strip.append({'k': k, 'at': a + i, 'ms': 1})

    return {'from': a, 'to': b, 'total': total, 'totals': totals, 'steps': steps, 'device': dev, 'strip': strip,
            'trips': len(trips), 'startsAt': 'CSRF fetch' if csrf else f'{act} call', 'action': act, 'summary': summary}


def render(data):
    """Fill the HTML template with the timeline data."""
    payload = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    return TEMPLATE.read_text(encoding='utf-8').replace('/*__DATA__*/null', payload, 1)


def main(argv=None):
    p = argparse.ArgumentParser(description='Turn a Mobile Services HAR (+ optional MDK client log) into a timeline page.')
    p.add_argument('har', type=Path, help='network trace (.har) downloaded from the Mobile Services cockpit')
    p.add_argument('clientlog', type=Path, nargs='?', help='client log (.json) of the same run (optional)')
    p.add_argument('-o', '--out', type=Path, help='write the HTML here (default: stdout)')
    p.add_argument('--title', default='MDK app - network timeline')
    p.add_argument('--action', help='URL pattern of the online call to break down (default: auto-detect)')
    p.add_argument('--download', help='URL pattern of the offline read(s) to follow end to end (default: auto-detect)')
    p.add_argument('--device-offset-ms', type=int, help='device clock offset, instead of the support-log marker')
    a = p.parse_args(argv)
    har = json.loads(a.har.read_text(encoding='utf-8-sig'))
    log = json.loads(a.clientlog.read_text(encoding='utf-8-sig')) if a.clientlog else None
    try:
        html = render(build(har, log, title=a.title, har_name=a.har.name, log_name=a.clientlog.name if log else '',
                            device_offset_ms=a.device_offset_ms, action=a.action, download=a.download))
    except ValueError as e:
        p.error(str(e))
    if a.out:
        a.out.write_text(html, encoding='utf-8')
    else:
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        print(html)


if __name__ == '__main__':
    main()
