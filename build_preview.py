import json

snap = json.load(open("/tmp/snap.json"))
css = open("app/static/styles.css").read()
data_json = json.dumps(snap["dash"])
opts = snap["opts"]
coverage = f"{len(opts['stores'])} stores &middot; data {opts['date_min']} &rarr; {opts['date_max']}"

# JS uses a placeholder __DATA__ so we never mix Python f-strings with JS braces
js = r"""
const D = __DATA__;
const n = v => v==null?'\u2014':Number(v).toLocaleString('en-IN');
const pct = v => v==null?'\u2014':Number(v).toFixed(2)+'%';
const gc = p => p>=5?'var(--bad)':p>=2?'var(--alert)':p>=1?'var(--cold)':'var(--good)';
const h = D.headline;
const worst = D.categories.filter(c=>c.dispatched>300).sort((a,b)=>b.gap_pct-a.gap_pct)[0];

document.getElementById('kpis').innerHTML =
'<div class="kpi hero"><div class="kpi-label">Claimable GRN gap</div><div class="kpi-val">'+pct(h.claimable_pct)+'</div>'+
'<div class="kpi-sub">target <b>0.20%</b> \u2014 dispatched less received, damage excluded</div>'+
'<div class="track"><div class="fill" style="width:13%"></div></div></div>'+
'<div class="kpi"><div class="kpi-label">Units unaccounted</div><div class="kpi-val">'+n(h.claimable_units)+'</div>'+
'<div class="kpi-sub">of '+n(h.dispatched)+' dispatched</div></div>'+
'<div class="kpi"><div class="kpi-label">Warehouse fulfilment</div><div class="kpi-val">'+pct(h.fulfillment_pct)+'</div>'+
'<div class="kpi-sub">'+n(h.fulfillment_gap_units)+' units never left \u2014 not claimable</div></div>'+
'<div class="kpi"><div class="kpi-label">Worst category</div>'+
'<div class="kpi-val" style="font-size:20px;color:'+gc(worst.gap_pct)+'">'+worst.category+'</div>'+
'<div class="kpi-sub">'+pct(worst.gap_pct)+' \u00b7 '+n(worst.claimable_units)+' units</div></div>';

const pl=h.ordered-h.picked, tl=h.dispatched-h.received;
function stg(v,l,loss,cls,end){
  return '<div class="fstage"><div class="fval"'+(end?' style="color:var(--good)"':'')+'>'+n(v)+'</div>'+
    '<div class="flabel">'+l+'</div><div class="fbar '+(end?'end':'')+'"></div>'+
    (loss>0?'<div class="floss '+cls+'">\u2212'+n(loss)+' '+(cls=='big'?'at picking':'in transit / at store')+'</div>':'')+'</div>';
}
document.getElementById('funnel').innerHTML =
  stg(h.ordered,'Ordered by stores',0,'')+'<div class="farrow">\u2192</div>'+
  stg(h.picked,'Picked & dispatched',pl,'big')+'<div class="farrow">\u2192</div>'+
  stg(h.received,'Received at store',tl,'small')+'<div class="farrow">\u2192</div>'+
  stg(h.received-h.damaged,'Sellable on shelf',0,'',true);

const mx=Math.max.apply(0,D.stores.map(s=>s.gap_pct).concat(1));
document.getElementById('storeTbl').innerHTML =
  '<thead><tr><th>Store</th><th>Flagged</th><th>Gap %</th><th>Units</th><th>Status</th></tr></thead><tbody>'+
  D.stores.slice(0,10).map(function(s){
    var stat = s.status=='repeat'?'crit':s.status=='watch'?'warn':'ok';
    var lbl = s.status=='repeat'?'Repeat':s.status=='watch'?'Watch':'Clean';
    return '<tr><td class="name">'+s.name+'</td><td>'+s.days_flagged+'/'+s.days_total+'</td>'+
      '<td><span class="heat"><i style="width:'+(s.gap_pct/mx*100)+'%;background:'+gc(s.gap_pct)+'"></i></span>'+pct(s.gap_pct)+'</td>'+
      '<td>'+n(s.claimable_units)+'</td><td><span class="pill '+stat+'">'+lbl+'</span></td></tr>';
  }).join('')+'</tbody>';

const cmx=Math.max.apply(0,D.categories.map(c=>c.gap_pct).concat(1));
document.getElementById('catList').innerHTML = D.categories.slice(0,8).map(function(c){
  return '<div class="catrow"><div class="catname">'+c.category+'</div>'+
    '<div class="catbar"><i style="width:'+(c.gap_pct/cmx*100)+'%;background:'+gc(c.gap_pct)+'"></i></div>'+
    '<div class="catval"><b>'+pct(c.gap_pct)+'</b><br><span style="font-size:10px">'+n(c.claimable_units)+' u</span></div></div>';
}).join('');

document.getElementById('prodBody').innerHTML = D.products.slice(0,12).map(function(p){
  return '<tr><td class="name">'+(p.description||p.fsn)+'</td><td class="name">'+(p.category||'\u2014')+'</td>'+
    '<td>'+n(p.stores_affected)+'</td><td>'+n(p.dispatched)+'</td><td>'+n(p.claimable_units)+'</td>'+
    '<td style="color:'+gc(p.gap_pct)+'">'+pct(p.gap_pct)+'</td></tr>';
}).join('');

document.getElementById('qualityList').innerHTML = D.quality.map(function(i){
  return '<div class="issue"><div class="sev '+i.severity+'"></div><div><b>'+i.title+'</b><p>'+i.detail+'</p></div></div>';
}).join('');

var sw = D.swaps.slice(0,4);
document.getElementById('swapList').innerHTML = sw.length ? sw.map(function(r){
  var badge = r.same_vehicle_pairs ? '<span class="pill crit">'+r.same_vehicle_pairs+' same-vehicle</span>' : '<span class="pill info">no route link</span>';
  return '<div style="border-bottom:1px solid rgba(42,51,61,.55);padding:12px 0">'+
    '<div style="display:flex;justify-content:space-between;flex-wrap:wrap"><b>'+(r.description||r.fsn)+'</b>'+badge+'</div>'+
    '<div class="mono muted" style="font-size:11.5px;margin-top:6px">short '+n(r.total_short)+' across '+r.stores_short+' \u00b7 excess '+n(r.total_excess)+' at '+r.stores_excess+'</div></div>';
}).join('') : '<div class="empty">No excess/shortage pairs.</div>';
"""

js = js.replace("__DATA__", data_json)

html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shri Balaji Ops - Live Preview</title><style>""" + css + """</style></head><body>
<div class="topbar"><div class="brand"><div class="mark">SB</div>
<div><b>Shri Balaji Ops</b><span>""" + coverage + """</span></div></div>
<div class="topbar-right"><span>Nitesh</span><span class="role-chip">admin</span><a style="cursor:pointer">Sign out</a></div></div>
<div class="tabs"><div class="tab active">Dashboard</div><div class="tab">Products</div>
<div class="tab">Route &amp; Swaps</div><div class="tab">Rejects</div><div class="tab">Data Quality</div>
<div class="tab">My Views</div><div class="tab">Upload</div><div class="tab">Alerts</div><div class="tab">Team</div></div>
<div class="view active" style="max-width:1500px;margin:0 auto">
<div class="filterbar"><div class="filter-head"><div class="filter-title">Filters - every number below recalculates</div>
<div class="filter-actions"><select style="width:auto;padding:7px 10px;font-size:12px"><option>Last 7 days</option></select>
<button class="btn sm ghost">Reset</button><button class="btn sm">Apply</button></div></div>
<div class="filter-grid">
<div class="field" style="margin:0"><label>From</label><input type="date"></div>
<div class="field" style="margin:0"><label>To</label><input type="date"></div>
<div class="field" style="margin:0"><label>Category</label><button class="ms-btn"><span>All</span><span></span></button></div>
<div class="field" style="margin:0"><label>Brand</label><button class="ms-btn"><span>All</span><span></span></button></div>
<div class="field" style="margin:0"><label>Store</label><button class="ms-btn"><span>All</span><span></span></button></div>
<div class="field" style="margin:0"><label>Vehicle</label><button class="ms-btn"><span>All</span><span></span></button></div>
</div></div>
<div id="kpis" class="kpis"></div>
<div class="panel"><div class="panel-head"><div><div class="panel-title">Packet journey</div>
<div class="panel-sub">Ordered &rarr; picked &rarr; received, across the batching and GRN files. The two losses are different problems: what never left the warehouse isn't claimable against Flipkart.</div></div></div>
<div class="funnel" id="funnel"></div></div>
<div class="grid2"><div class="panel"><div class="panel-head"><div><div class="panel-title">Store ranking</div>
<div class="panel-sub">Marked <b>Repeat</b> only when a store breaches on most of its loaded days with meaningful volume - one bad day is noise.</div></div></div>
<div class="tbl-wrap"><table id="storeTbl"></table></div></div>
<div class="panel"><div class="panel-head"><div><div class="panel-title">Category breakdown</div>
<div class="panel-sub">Claimable gap by category.</div></div></div><div id="catList"></div></div></div>
<div class="panel"><div class="panel-head"><div><div class="panel-title">Product-level shortage</div>
<div class="panel-sub">A product short at nearly every store the same day points upstream - to picking or counting - not to the stores.</div></div></div>
<div class="tbl-wrap"><table><thead><tr><th>Product</th><th>Category</th><th>Stores hit</th><th>Dispatched</th><th>Short</th><th>Gap %</th></tr></thead><tbody id="prodBody"></tbody></table></div></div>
<div class="panel"><div class="panel-head"><div><div class="panel-title">Excess / shortage cross-reference</div>
<div class="panel-sub">Same-vehicle pairs are worth a phone call; a shortage spread thin across many stores usually means an upstream count problem, not a crate swap. Evidence laid out - the call is yours.</div></div></div>
<div id="swapList"></div></div>
<div class="panel"><div class="panel-head"><div><div class="panel-title">Data quality</div>
<div class="panel-sub">Problems in the inputs, so a broken file is never mistaken for a broken store.</div></div></div>
<div id="qualityList"></div></div>
</div>
<script>""" + js + """</script></body></html>"""

open("/mnt/user-data/outputs/shri_balaji_live_preview.html", "w").write(html)
print("Live preview written:", len(html), "bytes")
