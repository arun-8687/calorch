import json
with open(r'C:\workspace\calorch\out\summary.json', encoding='utf-8') as f:
    s = json.load(f)
ev_by_label = {}
for ev in s['events']:
    cls = s['classifications'][ev['id']]
    ev_by_label.setdefault(cls['final_label'], []).append((ev, cls))

print('=== WMT 10-Q / META 8-K (earnings_call) ===')
for ev, cls in ev_by_label.get('earnings_call', [])[:1]:
    print('Subject :', ev['subject'])
    print('When    :', ev['start'])
    print('Link    :', ev['web_link'])
    print('Docx    :', s['documents'][ev['id']]['path'])
    print('Email   :', s['emails'][ev['id']]['status'], '->', s['emails'][ev['id']]['to'])
    print('Conf    :', cls['confidence'])
    print('Rationale:', cls['rationale'])
print()
print('=== Sample 4 (analyst_meeting) ===')
for ev, cls in ev_by_label.get('analyst_meeting', [])[:1]:
    print('Subject :', ev['subject'])
    print('When    :', ev['start'])
    print('Link    :', ev['web_link'])
print()
print('=== Form 4 example (HD) ===')
for ev, cls in ev_by_label.get('analyst_meeting', []):
    if 'HD' in ev['subject'] or 'HOME DEPOT' in ev['subject']:
        print('Subject :', ev['subject'])
        print('Body    :', ev['body_preview'][:200])
        break
print()
print('=== Sample XOM DEFA14A (management_meeting) ===')
for ev, cls in ev_by_label.get('management_meeting', [])[:1]:
    print('Subject :', ev['subject'])
    print('When    :', ev['start'])
    print('Link    :', ev['web_link'])
print()
print('=== First unknown filing ===')
for ev, cls in ev_by_label.get('unknown', [])[:1]:
    print('Subject :', ev['subject'])
    print('Body    :', ev['body_preview'][:200])
print()
# Follow-ups
print('=== Sample follow-up ===')
print(json.dumps(s['followups'][0], indent=2, default=str))
print()
print('=== Briefing path ===')
print(s['weekly_briefing_path'])
