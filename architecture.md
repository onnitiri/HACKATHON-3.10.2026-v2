# CellShield – Arkkitehtuuridokumentaatio

> **PEMFC cathode U-bend optimizer**  
> Hackathon 3.10.2026 · Quanscient Allsolve SDK challenge

---

## Sisällysluettelo

1. [Yleiskuvaus](#1-yleiskuvaus)
2. [Tiedostorakenne](#2-tiedostorakenne)
3. [Tietovirta (Data Flow)](#3-tietovirta-data-flow)
4. [Fysiikkamoduuli – physics.py](#4-fysiikkamoduuli--physicspy)
5. [Käyttöliittymä – app.py](#5-käyttöliittymä--apppy)
6. [Quanscient Allsolve -integraatio](#6-quanscient-allsolve--integraatio)
7. [Kriittiset raja-arvot](#7-kriittiset-raja-arvot)
8. [Validointi](#8-validointi)
9. [Suunnittelupäätökset ja kompromissit](#9-suunnittelupäätökset-ja-kompromissit)
10. [SDK:n käytettävyyshavainnot](#10-sdkn-käytettävyyshavainnot)

---

## 1. Yleiskuvaus

CellShield on **interaktiivinen insinöörityökalu** vedyllä toimivan polttokennon (PEMFC) katodipuolen virtauskanavan optimointiin. Sovellus ratkaisee kolme kriittistä ongelmaa:

| Ongelma | Seuraus jos huomiotta | Mitataan |
|---|---|---|
| 🌊 **Veden tulviminen** (flooding) | Vesipisarat tukkivat kanavan → tehonmenetys | % kanavapinta-alasta, jossa \|u\| < 1 m/s |
| 🌡️ **Membraanin ylikuumeneminen** | Membraani vaurioituu (pinhole) > 85 °C | Huippulämpötila GDL-poikkileikkauksessa |
| 💨 **Painehäviö** | Kompressorin loistoteho kasvaa | Pa per U-mutka, kW koko pinossa |

Sovellus toimii **kahdella tasolla**:
1. **Nopea surrogaatti** (< 100 ms) – analyyttiset kaavat reaaliaikaiseen UI:hin
2. **3D pilviverifikaatio** (Quanscient Allsolve) – täysi Navier-Stokes + konjugaatti lämmönsiirto

---

## 2. Tiedostorakenne

```
HACKATHON 3.10/
├── app.py              # Streamlit-käyttöliittymä, visualisoinnit, KPI-kortit
├── physics.py          # Kaikki fysiikkamallinnukset + Allsolve SDK -integraatio
├── config.toml         # Streamlit-teema (tumma, sinertävä)
├── .env                # API-avaimet (ALLSOLVE_ACCESS_KEY, ALLSOLVE_SECRET_KEY)
├── requirements.txt    # Python-riippuvuudet
├── ARCHITECTURE.md     # Tämä tiedosto
└── Hackathon criterias and docs/
    └── hackathon_ohjeet.md
```

**Pääriippuvuudet:**
```
streamlit   – web UI framework
plotly      – interaktiiviset kaaviot
numpy       – numeerinen laskenta
allsolve    – Quanscient Allsolve Python SDK
python-dotenv
```

---

## 3. Tietovirta (Data Flow)

```
Käyttäjä (liukusäätimet: w, R_in, v_in, i, V_cell, n_bends, n_channels, n_cells)
        │
        ▼
Design(frozen dataclass)   ←── kaikki parametrit yhdessä hashattavassa objektissa
        │
        ├──► evaluate(design)                    ├──► cached_pareto(design)
        │    ├── hydraulics()                    │    ├── 102 pistettä (17×6)
        │    ├── thermal()                       │    └── pareto_front()
        │    ├── geometry()                      │
        │    ├── velocity_field()  (surrogate)   │
        │    ├── flooding_fraction()             │
        │    ├── membrane_map()                  │
        │    └── continuity_error()              │
        │         Result(dataclass)              │
        ▼                                        ▼
app.py UI
├── KPI-kortit:  T_max | Flooding% | ΔP/bend | P_comp
├── Tab 1: Velocity heatmap + 1 m/s flooding isoline
├── Tab 2: GDL cross-section + membrane T map
├── Tab 3: Pareto scatter (v_in × R_in sweep)
├── Engineering assessment (automaattiset varoitukset)
└── [Submit to Allsolve] → run_quanscient_allsolve(design)
             │
             ▼
    Quanscient Allsolve (pilvi)
    Geometria → Alueet → Materiaalit → Fysiikka → Mesh → Solve → Tulokset
```

---

## 4. Fysiikkamoduuli – physics.py

### 4.1 Design-dataluokka

```python
@dataclass(frozen=True)
class Design:
    w_mm: float       # kanavan leveys [mm]
    r_in_mm: float    # sisäsäde [mm]
    v_in: float       # sisääntulovirtausnopeus [m/s]
    i_acm2: float     # virrantiheys [A/cm²]
    v_cell: float     # kennojännite [V]
    n_bends: int      # U-mutkien määrä per kanava
    n_channels: int   # rinnakkaiskanavat per kenno
    n_cells: int      # kennot pinossa
```

`frozen=True` → hashattava → `@st.cache_data` toimii automaattisesti.

**Geometrinen lukko:** `rib_width = 2 × R_in` — sisäribs on sidottu sisäsäteeseen.

---

### 4.2 Hydrauliikka – `hydraulics(d)`

| Laskenta | Kaava | Lähde |
|---|---|---|
| Hydraulinen halkaisija | `D_h = 2wh/(w+h)` | Standardi |
| Reynolds-luku | `Re = ρ·v·D_h/μ` | |
| Dean-luku | `De = Re·√(D_h/2R_c)` | Mutkavirtaus |
| f·Re (laminaari) | Shah & London 1978, 5. asteen polynomi | |
| Kitkahäviö | `ΔP_fric = (f·Re/Re)·(L_c/D_h)·(ρv²/2)` | Darcy-Weisbach |
| Mutkahäviö | Idelchik 180°: `K = 1.4·B1(R_c/D_h)` | Idelchik 2007 |
| Kompressorin teho | `P = ΔP_ch · Q_stack / η_comp` | |

---

### 4.3 Lämpömalli – `thermal(d)`

Täysin analyyttinen. Membraanin huippulämpötila:

```
T_max = T_coolant + ΔT_contact + ΔT_through + ΔT_lateral
```

| Komponentti | Kaava | Fysiikka |
|---|---|---|
| Lämpövuo | `q = i · (E_tn - V_cell)` | Elektrochemical heat |
| Kontaktivastus | `ΔT_contact = q · R_contact` | GDL–rib rajapinta |
| Läpäisy | `ΔT_through = q · t_GDL / k_⊥` | Pystysuora johtuminen |
| **Lateraalinen** | `ΔT_lat = q · w² / (8 · k_∥ · t_GDL)` | Kanavan keskeltä ribbaan |

> **Kriittinen havainto:** `ΔT_lat ∝ w²`  
> Kanavan leveyden kaksinkertaistaminen **nelinkertaistaa** lateraalisen lämpögradientin.  
> Siksi kapeat kanavat ovat termisesti suotuisia.

---

### 4.4 Surrogaatti-virtauskenttä – `velocity_field(d, geo, hyd)`

Analyyttinen malli kolmella fysikaalisella komponentilla:

```
g(η, s) = perusprofiili(η) × Dean-vino(s) × (1 - erotuskupla(η, s))
```

1. **Perusprofiili** – `1 - |2η-1|^n`, missä n kasvaa matalissa kanavissa
2. **Dean-kierto** – virtaus kallistuu ulkoseinälle mutkassa, De/200 skalaus
3. **Erotuskupla** – matalapaineinen alue sisäseinällä mutkan jälkeen:
   - Alkamiskynnys: sigmoid Dean-luvun ympärillä `De ≈ 30`
   - Kuplapituus: `L_r = 0.05 · De · √(w/R) · w`

**Jatkuvuusehto:** jokainen poikkileikkaus normalisoidaan → kokonaismassavirta säilyy.

---

### 4.5 Pareto-optimointi

**`pareto_sweep(d)`** – 17 × 6 = 102 pistettä (v_in × R_in), fields=False → ~100× nopeampi.

**`pareto_front(pts)`** – non-dominated set minimoiden (ΔP_unit, flooding%).

---

## 5. Käyttöliittymä – app.py

### Presetit

| Preset | i [A/cm²] | v_in [m/s] | V_cell [V] |
|---|---|---|---|
| High Load (Critical) | 2.0 | 3.5 | 0.60 |
| Nominal Load | 1.2 | 2.5 | 0.65 |
| Low Load / Idle | 0.5 | 1.2 | 0.78 |

### Välilehdet

| Tab | Sisältö | Plotly |
|---|---|---|
| Velocity & Flooding | 2D nopeuskenttä + punainen 1 m/s isoviiva | Heatmap + Contour |
| MEA Temperature | GDL poikkileikkaus + membraani in-plane T | Contour + Heatmap |
| Pareto Trade-off | Tulviminen vs. paine (v_in × R_in) | Scatter |

---

## 6. Quanscient Allsolve -integraatio

**`run_quanscient_allsolve(design, wait_for_results=False)`**

Koko putkilinja ei koskaan nosta poikkeusta – palauttaa aina `{"status": ..., "message": ...}`.

### 6.1 Geometrian rakentaminen (CSG Boolean ops)

Koordinaatisto: X = jalan suunta, Y = poikittainen, Z = syvyys.

```
leg1   = Box(x=0, y=0,       dx=L, dy=w,   dz=dep)  # 1. jalka
leg2   = Box(x=0, y=rib+w,   dx=L, dy=w,   dz=dep)  # 2. jalka (paluu)

bend_outer   = Cylinder(x=L, y=y_c, r=R_in+w, dz=dep)
bend_inner   = Cylinder(x=L, y=y_c, r=R_in,   dz=dep)
bend_annulus = Difference(bend_outer, [bend_inner])
clip_box     = Box(kattaa puoliavaruuden X≥L)
bend_channel = Intersection(bend_annulus, clip_box)  # 180° kaari

air_union = Union(leg1, [leg2, bend_channel])

gdl_vol = Box(x=0, y=0, z=-gdl, dx=L, dy=2w+rib, dz=gdl)

add_fragment_all([air_union, gdl_vol])  ← jakaa kosketuspinnat topologisesti
```

### 6.2 Alueet (Regions)

| Region | Tyyppi | Käyttö |
|---|---|---|
| `air_channel` | Volume [1] | LaminarFlow + HeatFluid |
| `gdl` | Volume [2] | HeatTransfer |
| `inlet` | Surface [1] | Sisääntuloehto |
| `outlet` | Surface [2] | Paineehto |
| `membrane` | Surface [3] | Lämpövuo |
| `coolant_plate` | Surface [4] | Kiinteä T |

### 6.3 Materiaalit

**Ilma 65°C** → density=1.04, μ=2.03e-5, cp=1008, k=0.029  
**GDL (Carbon paper)** → density=450, cp=710, k anisotrooppinen [[1.5, 0, 0], [0, 1.5, 0], [0, 0, 0.5]]

### 6.4 Fysiikka ja reunaehdot

| Fysiikka | SDK-luokka | Reunaehdot |
|---|---|---|
| `LaminarFlow` | `Physics.LaminarFlow(target=vol_air)` | Inlet: `VelocityConstraint([v_in,0,0])`, Outlet: `PressureConstraint(0)` |
| `HeatFluid` | `Physics.HeatFluid(target=vol_air)` | Inlet: `HeatFluidConstraint(338.15 K)` |
| `HeatTransfer` | `Physics.HeatTransfer(target=vol_gdl)` | Membrane: `HeatTransferHeatFlux(q)`, Coolant: `TemperatureConstraint(338.15 K)` |

### 6.5 Mesh ja simulaatio

```python
MeshSettings(mesh_size_max=w/6, mesh_size_min=w/20, max_run_time_minutes=30)
create_simulation_static(max_run_time_minutes=60, mesh=mesh)
```

### 6.6 Tulokset

```python
# Kenttätulokset
FieldOutput("velocity_magnitude",   "norm(velocity)")
FieldOutput("membrane_temperature", "temperature - 273.15", target=face_membrane)

# Skalaaritulokset
ValueOutput("max_velocity",       "maxOver(norm(velocity), air_channel)")
ValueOutput("max_temperature_C",  "maxOver(temperature - 273.15, membrane)")
```

---

## 7. Kriittiset raja-arvot

```python
T_WARN, T_CRIT     = 80.0, 85.0        # °C – membraanin lämpötilarajat
FLOOD_WARN, FLOOD_BAD = 15.0, 30.0     # % – tulvimisrajan prosentit
DP_WARN, DP_BAD    = 5_000, 10_000     # Pa – painehäviörajan
U_CRIT             = 1.0               # m/s – tulvimisnopeuden kynnys
E_TN               = 1.25              # V   – termoneutrajännite (LHV)
T_COOLANT          = 65.0              # °C  – jäähdytyslämpötila
ETA_COMP           = 0.6               # –   – kompressorin hyötysuhde
```

---

## 8. Validointi

| Tarkistus | Menetelmä | Tarkkuus |
|---|---|---|
| f·Re | Shah & London vs. Fourier-sarja (60 termiä) | < 0.5% |
| Laminaarisuus | Re < 2000 | binäärinen |
| Jatkuvuus | Max fluksivirhe poikkileikkauksissa | < 1% |
| Mutkahäviö | Idelchik K-kerroin | % ΔP:stä |
| Terminen budjetti | ΔT-komponenttien summa | = T_max - T_coolant |

---

## 9. Suunnittelupäätökset ja kompromissit

### Miksi surrogaatti eikä suoraan CFD?

Slider-muutos → tulos < 100 ms. 3D CFD kestää 5–60 min. Surrogaatti mahdollistaa **reaaliaikaisen parametritutkimisen** ennen kallista pilvisimulointia.

### Miksi frozen dataclass?

`@st.cache_data` vaatii hashattavia argumentteja. `frozen=True` dataclass on automaattisesti hashattava. Pareto-laskenta cachetetaan: uudelleenajo vain kun relevantti parametri muuttuu.

### Miksi rib = 2·R_in?

Geometrinen välttämättömyys: U-mutkan kaksi jalkaa mahtuvat vierekkäin vain kun ribbalileveys ≥ 2·R_in. Tämä vähentää vapausasteita ja estää fysikaalisesti mahdottomia kombinaatioita.

### Miksi HeatFluid eikä HeatTransfer ilmakanavassa?

`HeatFluid` mallintaa **konvektiivisen lämmönsiirron** fluideissa (lämpöenergia kulkeutuu virtauksen mukana). `HeatTransfer` on puhtaasti johtumiselle (solideille). Konjugaatti-ongelma vaatii molemmat.

---

## 10. SDK:n käytettävyyshavainnot

### Toimii hyvin

- Automaattisesti generoidut BC-luokat (`LaminarFlowVelocityConstraint` jne.) selkeästi nimetty
- `add_fragment_all()` tekee topologisesti oikean geometrian yhdellä kutsulla
- `frozen=True` dataclass + `@st.cache_data` toimii saumattomasti

### Parannusehdotukset

| Ongelma | Ehdotus |
|---|---|
| **Entity-tagit tuntemattomia etukäteen** – GMSH numeroi pinnat rakennusjärjestyksessä | Lisää `geo.get_face_by_position(x,y,z)` tai nimikennot geometriabuilderiin |
| **`GeometryPipelineVersion.V2` ei dokumentoitu** selkeästi missä vaaditaan | Tee V2 oletukseksi tai lisää selkeä virheilmoitus |
| **`add_cylinder()` akselin suunta** epäselvä – onko x,y,z pohja vai akselin piste? | Paranna docstring: "x,y,z = bottom center of cylinder axis" |
| **`sim.wait()` ei tue timeout/progress** | Lisää `sim.wait(timeout_minutes=60, on_progress=callback)` |
| **Pitkät parametrinimet** kuten `laminar_flow_velocity_constraint` | Harkitse lyhyitä aliasoja tai keyword-only `value=` |

---

## Liite: Fysikaaliset vakiot

```python
U_CRIT    = 1.0       # m/s   – tulvimiskynnys
T_COOLANT = 65.0      # °C    – jäähdytyslämpötila
K_GDL_IP  = 1.5       # W/(m·K) – GDL in-plane johtavuus
K_GDL_TP  = 0.5       # W/(m·K) – GDL through-plane johtavuus
T_GDL     = 0.2e-3    # m     – GDL paksuus
R_CONTACT = 1.5e-4    # m²K/W – kontaktivastus GDL-rib
DEPTH     = 0.5e-3    # m     – kanavan syvyys
LEG       = 10.0e-3   # m     – suoran jalan pituus
E_TN      = 1.25      # V     – termoneutraalijännite (LHV)
ETA_COMP  = 0.6       # –     – kompressorin hyötysuhde
AIR_RHO   = 1.04      # kg/m³ – ilman tiheys ~65°C
AIR_MU    = 2.03e-5   # Pa·s  – ilman dynaaminen viskositeetti ~65°C
DE_SEP    = 30.0      # –     – Dean-luku jolla sisäseinän erotus alkaa
```

---

*Dokumentaatio generoitu 3.10.2026 · CellShield v1.0*
