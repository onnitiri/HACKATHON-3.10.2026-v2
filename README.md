# CellShield – U-mutkan virtauskenttä

Syötä kanavan leveys, mutkan sisäsäde ja tulonopeus. "Aja Allsolve-simulaatio" laskee 2D-laminaarivirtauksen
U-mutkassa Quanscient Allsolvessa (noin minuutti) ja näyttää nopeuskentän. Ripaleveys on lukittu: w_rib = 2 · Rin.

`default.json` on valmiiksi laskettu Allsolve-tulos oletusarvoille (w = 1,0 mm, Rin = 0,6 mm, vin = 2,5 m/s).

## Käynnistys

```bash
cd cellshield
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
cp .env.example .env          # ALLSOLVE_ACCESS_KEY / ALLSOLVE_SECRET_KEY
.venv/bin/python server.py 8080
# avaa http://localhost:8080
```

## Tiedostot

```
index.html                 käyttöliittymä
server.py                  paikallinen palvelin: sivu + /api/verify (avaimet pysyvät palvelimella)
api/_allsolve_pipeline.py  Allsolve-malli: geometria → alueet → materiaali → virtaus → mesh → simulaatio → nopeuskenttä
default.json               tallennettu tulos oletusarvoille
```

## Malli

2D, ilma 65 °C (ρ = 1,04 kg/m³, μ = 2,03e-5 Pa·s), täysi laminaarinen Navier–Stokes (Newton-iteraatio).
Tulo vasemman haaran yläpäässä (nopeus vin alaspäin), ulostulo oikean haaran yläpäässä (p = 0), seinillä no-slip.
Haaran pituus 10 mm.

**Vesitasku (kuollut alue)** = kohta, jossa virtaus kääntyy takaisin kanavan suuntaa vastaan, tai jossa ilma liikkuu
alle 1 m/s yli 15 %:n päässä kanavan leveydestä seinästä (seinän vieressä ilma on aina hidasta, eikä sitä lasketa).
Hyväksytty alle 1 %, rajalla 1–5 %, hylätty yli 5 % kanavan pinta-alasta. Kuvaaja näyttää taskun punaisena ja nuoli
osoittaa suurimman taskun sijainnin.

Jos Navier–Stokes ei suppene (tiukka mutka ja suuri nopeus), tulosta ei näytetä.

Esimerkit (`examples/`, oikeita Allsolve-ajoja):
- onnistuu: w 0,8 mm, Rin 1,0 mm, vin 3,0 m/s → vesitaskuja 0 %
- epäonnistuu: w 1,6 mm, Rin 0,2 mm, vin 3,0 m/s → 10,3 %, paluuvirtaus mutkan jälkeen sisäseinällä

SDK-huomiot (allsolve 0.5.2):
- `LaminarFlow` ei ratkaise virtausta ilman `LaminarFlowLinear`-interaktiota, ja se vaatii materiaalille äänennopeuden.
- `LaminarFlowLinear` tuottaa Stokes-yhtälön (ei inertiaa). Täysi Navier–Stokes otetaan käyttöön mukautetulla
  skriptillä (`AFTER_FORMULATIONS_CREATED`, `qs.predefinednavierstokes`).
- Boolen operaation tulos säilyttää ensimmäisen kappaleen CAD-nimen (`bend_outer`).
