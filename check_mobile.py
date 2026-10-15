import yaml, re

with open(r'C:\Users\zxf72\proxy_collector\output-mobile.yaml', 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f)

uuid_pat = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
bad = []
for i, p in enumerate(data['proxies']):
    if p.get('type') == 'ss' and 'cipher' in p:
        c = p['cipher']
        if uuid_pat.match(c):
            bad.append((i, p['name'][:60], c[:30]))

print(f'Total proxies: {len(data["proxies"])}')
print(f'Total SS: {sum(1 for p in data["proxies"] if p["type"]=="ss")}')
print(f'Bad cipher (UUID): {len(bad)}')
for i, n, c in bad[:15]:
    print(f'  [{i}] {n}')
    print(f'       cipher: {c}')
