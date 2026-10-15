import yaml

with open(r'C:\Users\zxf72\.config\clash\config.yaml', 'r', encoding='utf-8') as f:
    f.readline()
    data = yaml.safe_load(f)

# proxy 4 = index 3
p = data['proxies'][3]
print("Proxy 4:")
for k, v in p.items():
    val_str = repr(v)
    if len(val_str) > 80:
        val_str = val_str[:80] + '...'
    print(f"  {k}: {val_str}")

# Also check proxy 2,3,5 for reality-opts
for idx in [1, 2, 3, 4]:
    p = data['proxies'][idx]
    has_reality = 'reality-opts' in p
    name_short = p['name'][:50]
    print(f"\nProxy {idx+1}: {name_short}")
    print(f"  type={p['type']}, has_reality={has_reality}")
    if has_reality:
        ro = p['reality-opts']
        print(f"  reality: pk={ro.get('public-key','?')[:20]}..., sid={ro.get('short-id','?')}")
