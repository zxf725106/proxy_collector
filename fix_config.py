import yaml, re

with open(r'C:\Users\zxf72\proxy_collector\output-filtered.yaml', 'r', encoding='utf-8') as f:
    f.readline()
    data = yaml.safe_load(f)

# Check all SS proxies for invalid cipher
uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
bad_ss = 0
other_bad_cipher = 0

known_ciphers = {
    'aes-128-gcm', 'aes-192-gcm', 'aes-256-gcm',
    'chacha20-ietf-poly1305', 'chacha20-ietf', 'chacha20',
    'xchacha20-ietf-poly1305', 'xchacha20',
    'aes-128-ctr', 'aes-192-ctr', 'aes-256-ctr',
    'aes-128-cfb', 'aes-192-cfb', 'aes-256-cfb',
    'rc4-md5', 'bf-cfb', 'salsa20', 'camellia-128-cfb',
    'camellia-192-cfb', 'camellia-256-cfb', 'idea-cfb',
    'rc2-cfb', 'seed-cfb', 'dummy',
    '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm',
    '2022-blake3-chacha20-poly1305',
}

for p in data['proxies'][:]:
    if p.get('type') == 'ss' and 'cipher' in p:
        c = p['cipher']
        if uuid_pattern.match(c):
            bad_ss += 1
            print(f"SS bad cipher (UUID): {p['name'][:60]}")
            data['proxies'].remove(p)
            continue
        if c not in known_ciphers and 'password' not in p:
            other_bad_cipher += 1
            print(f"SS unknown cipher: {c} -> {p['name'][:60]}")

# Clean proxy-groups
proxy_names = {p['name'] for p in data['proxies']}
for g in data.get('proxy-groups', []):
    g['proxies'] = [n for n in g.get('proxies', []) if n in proxy_names]

with open(r'C:\Users\zxf72\proxy_collector\output-filtered-fixed.yaml', 'w', encoding='utf-8') as f:
    f.write(f"# Fixed SS cipher + reality validation - {len(data['proxies'])} proxies\n")
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

print(f"\nRemoved {bad_ss} SS with UUID-as-cipher")
print(f"Other unknown ciphers: {other_bad_cipher}")
print(f"Kept: {len(data['proxies'])} proxies")
