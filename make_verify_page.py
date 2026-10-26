#!/usr/bin/env python3
"""Build verify/index.html: label image next to the values stored in foods.db, for every image-read row.
Images are copied from the local cache and are NOT meant for the public repo (verify/ is git-ignored)."""
import json, random, sqlite3, sys
from io import BytesIO
from html import escape
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import scrape_foods as sf
from PIL import Image

OUT = sf.HERE / "verify"
(OUT / "img").mkdir(parents=True, exist_ok=True)
conn = sf.db()
rows = conn.execute("""SELECT p.id, p.handle, p.brand, p.name, p.label_urls, p.servings_per_container sv, p.serving_desc sd,
                              n.* FROM products p JOIN nutrition n ON n.product_id=p.id
                       WHERE p.kind='meal' AND n.source='manual' AND p.label_urls IS NOT NULL AND p.label_urls!='[]'
                       ORDER BY p.brand, p.name""").fetchall()
FIELDS = [("calories", "Calories", ""), ("total_fat_g", "Total fat", "g"), ("sat_fat_g", "Sat fat", "g"), ("trans_fat_g", "Trans fat", "g"),
          ("cholesterol_mg", "Cholesterol", "mg"), ("sodium_mg", "Sodium", "mg"), ("carbs_g", "Total carb", "g"), ("fiber_g", "Fiber", "g"),
          ("sugars_g", "Total sugars", "g"), ("added_sugars_g", "Added sugars", "g"), ("protein_g", "Protein", "g"),
          ("vitamin_d_mcg", "Vitamin D", "mcg"), ("calcium_mg", "Calcium", "mg"), ("iron_mg", "Iron", "mg"), ("potassium_mg", "Potassium", "mg")]
items = []
for r in rows:
    data = sf.get(sf.strip_size(json.loads(r["label_urls"])[0]), binary=True, subdir="labels")
    if not data:
        continue
    im = Image.open(BytesIO(data)).convert("RGB")
    if im.width >= 1800 and r["brand"].startswith("Backpacker"):
        im = sf.find_label_crop(im) or im
    im.thumbnail((1100, 1500))
    fn = f"img/{r['handle']}.jpg"
    im.save(OUT / fn, quality=82)
    basis = r["basis"]
    vals = "".join(f"<tr><td>{lab}</td><td>{'' if r[k] is None else (f'{r[k]:g}' + u)}</td></tr>" for k, lab, u in FIELDS)
    items.append(dict(handle=r["handle"], brand=r["brand"], name=r["name"], fn=fn, vals=vals, basis=basis, sv=r["sv"], sd=r["sd"] or ""))

random.seed(20261009)
sample = set(random.sample([i["handle"] for i in items], 15))

def card(i):
    return f"""<section class="card" data-h="{escape(i['handle'])}" id="{escape(i['handle'])}">
<header><b>{escape(i['brand'])}</b> &middot; {escape(i['name'])} <code>{escape(i['handle'])}</code></header>
<div class="grid"><a href="{i['fn']}" target="_blank"><img loading="lazy" src="{i['fn']}" alt="label"></a>
<div><p class="meta">Stored as <b>{escape(i['basis'])}</b> basis &middot; servings/container: {i['sv']:g} &middot; {escape(i['sd'])}</p>
<table>{i['vals']}</table>
<div class="v"><label><input type="radio" name="v-{escape(i['handle'])}" value="ok"> matches</label>
<label><input type="radio" name="v-{escape(i['handle'])}" value="bad"> error</label>
<input class="note" placeholder="what's wrong"></div></div></div></section>"""

s_items = [i for i in items if i["handle"] in sample]
r_items = [i for i in items if i["handle"] not in sample]
html = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Label vs database check</title>
<style>
:root{{--bg:#fff;--fg:#1c1c1c;--mut:#666;--line:#ddd;--ok:#1a7f37;--bad:#b42318}}
@media(prefers-color-scheme:dark){{:root{{--bg:#161616;--fg:#eee;--mut:#aaa;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
.card{{border:1px solid var(--line);border-radius:8px;margin:14px 0;padding:10px}}
.card.ok{{border-color:var(--ok)}} .card.bad{{border-color:var(--bad)}}
.grid{{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:14px}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
img{{width:100%;height:auto;background:#fff;border:1px solid var(--line)}}
table{{border-collapse:collapse;width:100%}} td{{border-bottom:1px solid var(--line);padding:2px 6px}} td+td{{text-align:right;font-variant-numeric:tabular-nums}}
.meta,.mut{{color:var(--mut);font-size:13px}} code{{color:var(--mut);font-size:12px}}
.v{{margin-top:8px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}} .note{{flex:1;min-width:140px}}
.bar{{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:2}}
button{{font:inherit;padding:4px 10px}}
</style>
<div class="bar"><b>Label image vs stored values</b> &nbsp;<span id="tally" class="mut"></span> &nbsp;<button id="exp">Copy results</button></div>
<p class="mut">Values were read from the label images by a generative AI model (not OCR, not typed by hand). Column used: <b>per container</b> where the label has two columns. The first {len(s_items)} cards are a fixed random sample (seed 20261009); the remaining {len(r_items)} follow. Choices save in this browser only.</p>
<h2>Sample ({len(s_items)})</h2>{''.join(card(i) for i in s_items)}
<h2>All other image-read rows ({len(r_items)})</h2>{''.join(card(i) for i in r_items)}
<script>
const K='labelcheck-v1';let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}}}}
function tally(){{const v=Object.values(st);const ok=v.filter(x=>x.v==='ok').length,bad=v.filter(x=>x.v==='bad').length;
 document.getElementById('tally').textContent=ok+' match, '+bad+' error, '+(document.querySelectorAll('.card').length-ok-bad)+' unchecked'}}
document.querySelectorAll('.card').forEach(c=>{{const h=c.dataset.h,s=st[h]||{{}};
 if(s.v){{const r=c.querySelector('input[value='+s.v+']');if(r)r.checked=true;c.classList.add(s.v)}}
 c.querySelector('.note').value=s.n||'';
 c.addEventListener('change',e=>{{const v=(c.querySelector('input[type=radio]:checked')||{{}}).value;st[h]={{v:v,n:c.querySelector('.note').value}};
  c.classList.remove('ok','bad');if(v)c.classList.add(v);save();tally()}});
 c.querySelector('.note').addEventListener('input',()=>{{st[h]=Object.assign(st[h]||{{}},{{n:c.querySelector('.note').value}});save()}});}});
document.getElementById('exp').onclick=()=>{{const t=JSON.stringify(st,null,1);navigator.clipboard.writeText(t).then(()=>alert('Copied '+Object.keys(st).length+' results'),()=>prompt('Copy:',t))}};
tally();
</script>"""
(OUT / "index.html").write_text(html, encoding="utf-8")
print(f"{len(items)} cards ({len(s_items)} sample) -> {OUT/'index.html'}")
