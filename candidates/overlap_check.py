import yaml

def fps(path):
    try:
        d = yaml.safe_load(open(path, encoding="utf-8"))
    except Exception as e:
        print("ERR", path, e); return set()
    s = set()
    for p in d.get("proxies", []) or []:
        key = (p.get("type"), p.get("server"), p.get("port"))
        s.add(key)
    return s

existing = fps("output.yaml")
print(f"existing output.yaml fingerprints: {len(existing)}")

for name, path in [("rico-cell","candidates/rico-cell.yaml"),
                   ("ruk1ng","candidates/ruk1ng-clash.yaml"),
                   ("hyqzj","candidates/hyqzj-clash.yaml")]:
    c = fps(path)
    inter = c & existing
    print(f"{name}: {len(c)} fps, overlap with existing={len(inter)} ({100*len(inter)/max(1,len(c)):.1f}%)")
