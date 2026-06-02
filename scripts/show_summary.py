import json
with open(r'C:\workspace\calorch\out\summary.json', encoding='utf-8') as f:
    s = json.load(f)
print('Events classified:')
for eid, c in s['classifications'].items():
    label = c['final_label']
    conf = c['confidence']
    routed = c['routed_node']
    print(f'  {eid} -> {label:<22}  conf={conf:.2f}  routed={routed}')
print()
print('Documents:')
for eid, d in s['documents'].items():
    print(f'  {eid}: {d["path"]}  {d["bytes"]} bytes')
print()
print('Emails:')
for eid, em in s['emails'].items():
    print(f'  {eid}: status={em["status"]}  to={em["to"]}  attachment={bool(em.get("attachment_path"))}')
print()
print('Follow-ups:', len(s['followups']))
print('Errors:', len(s['errors']))
print('Briefing:', s['weekly_briefing_path'])
print('Repository:')
import os
print('  ', os.path.getsize(r'C:\workspace\calorch\out\repository.json'), 'bytes')
