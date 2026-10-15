import yaml, re

with open(r'C:\Users\zxf72\proxy_collector\output-mobile.yaml', 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f)

stats = {'no_pk': 0, 'short_sid': 0, 'bad_cipher': 0, 'blake3_cipher': 0}
kept = []

for p in data['proxies']:
    # Reality validation
    if 'reality-opts' in p:
        ro = p['reality-opts']
        if not ro.get('public-key'):
            stats['no_pk'] += 1
            continue
        sid = str(ro.get('short-id', ''))
        if not re.match(r'^[0-9a-fA-F]{8,}$', sid):
            stats['short_sid'] += 1
            continue

    # SS cipher checks
    if p.get('type') == 'ss':
        c = p.get('cipher', '')
        # UUID as cipher -> bad
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', c, re.I):
            stats['bad_cipher'] += 1
            continue
        # 2022-blake3 ciphers require proper base64 key format, often broken
        if '2022-blake3' in c:
            stats['blake3_cipher'] += 1
            continue

    kept.append(p)

data['proxies'] = kept
proxy_names = {p['name'] for p in kept}
for g in data.get('proxy-groups', []):
    g['proxies'] = [n for n in g.get('proxies', []) if n in proxy_names]

with open(r'C:\Users\zxf72\proxy_collector\output-mobile-fixed.yaml', 'w', encoding='utf-8') as f:
    f.write(f"# Fixed mobile - {len(kept)} proxies\n")
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

print(f"Stats (removed): {stats}")
print(f"Kept: {len(kept)} proxies")
