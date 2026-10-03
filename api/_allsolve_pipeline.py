"""CellShield: 2D laminar air flow in one cathode U-bend, solved in Quanscient Allsolve.

Geometry (2D, metres): two vertical legs (length L, width w) joined by a 180° bend at the bottom.
Rib between the legs is locked to 2*R_in. Inlet at the top of the left leg, outlet at the top of the right leg.

Every public function returns a JSON-able dict {"status": ..., "message": ...} and never raises.
"""
from __future__ import annotations

import glob
import io
import os
import re
import tempfile
import traceback

LEG = 10e-3              # straight leg length [m]
RHO, MU, C_SOUND = 1.04, 2.03e-5, 368.0  # air at 65 °C
U_SLOW = 1.0             # below this air speed water can collect [m/s]
WALL_LAYER = 0.15        # near-wall layer (fraction of w) where slow air is normal (no-slip) and not counted
BACKFLOW_TOL = 0.02      # reverse flow counts when it exceeds this fraction of v_in (ignores round-off at walls)

# The SDK generates a Stokes (creeping-flow) form for LaminarFlow + Linear. Replace it with Allsolve's full steady
# Navier-Stokes form (Newton iteration) so inertia - and with it flow separation after the bend - is included.
NAVIER_STOKES_SCRIPT = (
    "form = qs.formulation()\n"
    "form += qs.integral(reg.air, qs.predefinednavierstokes(qs.dof(fld.V), qs.tf(fld.V), fld.V, "
    "qs.dof(fld.p), qs.tf(fld.p), par.Mu(), par.rho(), 0, 0))\n"
)

try:
    import allsolve  # type: ignore

    SDK_ERROR = None
except Exception as e:  # pragma: no cover
    allsolve = None
    SDK_ERROR = f"{type(e).__name__}: {e}"


def availability() -> dict:
    if allsolve is None:
        return {"status": "unavailable", "available": False, "message": f"Allsolve SDK is missing ({SDK_ERROR})."}
    if not (os.environ.get("ALLSOLVE_ACCESS_KEY") and os.environ.get("ALLSOLVE_SECRET_KEY")):
        return {"status": "unavailable", "available": False, "message": "Allsolve keys are not configured on the server."}
    return {"status": "ready", "available": True, "message": "Allsolve ready."}


def _client():
    cache = os.environ.get("ALLSOLVE_CACHE_DIR", "/tmp/allsolve-cache")
    os.makedirs(cache, exist_ok=True)
    kwargs = dict(api_key=os.environ["ALLSOLVE_ACCESS_KEY"], api_secret=os.environ["ALLSOLVE_SECRET_KEY"],
                  cache_base_dir=cache, dotenv_file=None)
    if os.environ.get("ALLSOLVE_HOST"):
        kwargs["host"] = os.environ["ALLSOLVE_HOST"]
    return allsolve.Client(**kwargs)


def _err(stage: str, e: Exception) -> dict:
    return {"status": "error", "message": f"{stage}: {type(e).__name__}: {e}", "trace": traceback.format_exc(limit=3)}


def _design(d: dict) -> dict:
    def f(k, lo, hi, default):
        try:
            return min(hi, max(lo, float(d.get(k, default))))
        except (TypeError, ValueError):
            return default
    return {"w_mm": f("w_mm", 0.4, 2.0, 1.0), "r_in_mm": f("r_in_mm", 0.2, 2.0, 0.6), "v_in": f("v_in", 0.5, 8.0, 2.5)}


def build_project(client, d: dict):
    w, r_in, v = d["w_mm"] * 1e-3, d["r_in_mm"] * 1e-3, d["v_in"]
    r_out = r_in + w
    e = 1e-6
    project = client.create_project(name=f"CellShield U-bend w={d['w_mm']:.2f} Rin={d['r_in_mm']:.2f} v={v:.2f}",
                                    description="CellShield: 2D laminar flow in a cathode U-bend", dimension=2)

    gb = project.geometry_builder()
    co = allsolve.CadAlignment.CORNER
    gb.add_rectangle(name="leg_in", position=(-r_out, 0), size=(w, LEG), alignment=co)
    gb.add_rectangle(name="leg_out", position=(r_in, 0), size=(w, LEG), alignment=co)
    gb.add_disk(name="bend_outer", position=(0, 0), radius=r_out)
    gb.add_disk(name="bend_inner", position=(0, 0), radius=r_in)
    # Boolean results keep the object's CAD name ("bend_outer"), so later steps reference that name.
    gb.add_difference(name="ring", cad_names_1=["bend_outer"], cad_names_2=["bend_inner"])
    gb.add_rectangle(name="lower_half", position=(-2 * r_out, -2 * r_out), size=(4 * r_out, 2 * r_out), alignment=co)
    gb.add_intersection(name="bend", cad_names_1=["bend_outer"], cad_names_2=["lower_half"])
    gb.add_union(name="channel", cad_names=["leg_in", "leg_out", "bend_outer"])
    gb.build(on_error=allsolve.OnError.RAISE)

    S, C, RO = allsolve.Region.SURFACE, allsolve.Region.CURVE, allsolve.RegionOperation
    air = project.create_region_rule(name="air", entity_type=S, bounding_box=((-r_out - e, -r_out - e, -e), (r_out + e, LEG + e, e)))
    inlet = project.create_region_rule(name="inlet", entity_type=C, bounding_box=((-r_out - e, LEG - e, -e), (-r_in + e, LEG + e, e)))
    outlet = project.create_region_rule(name="outlet", entity_type=C, bounding_box=((r_in - e, LEG - e, -e), (r_out + e, LEG + e, e)))
    boundary = project.create_region_computed(name="boundary", entity_type=C, operation=RO.BOUNDARY, source_regions=[air.id])
    ports = project.create_region_computed(name="ports", entity_type=C, operation=RO.UNION, source_regions=[inlet.id, outlet.id])
    walls = project.create_region_computed(name="walls", entity_type=C, operation=RO.DIFFERENCE, source_regions=[boundary.id, ports.id])

    project.create_material(name="Air 65C", target_region=air, density=RHO, dynamic_viscosity=MU, speed_of_sound=C_SOUND)

    ps = project.get_default_physics_set()
    flow = ps.add_physics(allsolve.Physics.LaminarFlow(target=air))
    I = allsolve.Interaction
    flow.add_interactions([
        # Without this domain interaction the SDK's LaminarFlow solves no flow inside the domain.
        I.LaminarFlowLinear(name="Newtonian fluid", target=air, laminar_flow_linear_compressible=False),
        I.LaminarFlowVelocityConstraint(name="Inlet", target=inlet, laminar_flow_velocity_constraint=f"[1, 0; 1, {-v}]"),
        I.LaminarFlowPressureConstraint(name="Outlet", target=outlet, laminar_flow_pressure_constraint="0"),
        I.LaminarFlowVelocityConstraint(name="Walls (no slip)", target=walls, laminar_flow_velocity_constraint="[1, 0; 1, 0]"),
    ])

    mesh = project.create_mesh(allsolve.MeshSettings(name="mesh", mesh_size_max=w / 12, mesh_size_min=w / 40, max_run_time_minutes=10))
    sim = project.create_simulation_static(name="flow", description="2D laminar flow", max_run_time_minutes=20, mesh=mesh, physics_set=ps)
    sim.set_scripts([allsolve.Script(name="navier_stokes.py", section_name=allsolve.CustomSection.AFTER_FORMULATIONS_CREATED,
                                     content=NAVIER_STOKES_SCRIPT)])
    sim.add_outputs([
        allsolve.Output.FieldOutput(name="vx", expression="compx(V)"),
        allsolve.Output.FieldOutput(name="vy", expression="compy(V)"),
        # outlet pressure is 0, so the mean inlet pressure is the pressure drop of the U-bend
        allsolve.Output.ValueOutput(name="dp", expression="average(reg.inlet, p, 3)"),
    ])
    return project, mesh, sim


def submit(design: dict) -> dict:
    a = availability()
    if not a["available"]:
        return a
    d = _design(design or {})
    try:
        client = _client()
        project, mesh, sim = build_project(client, d)
        mesh.start()
    except Exception as e:
        return _err("Model setup failed", e)
    return {"status": "submitted", "project_id": project.id, "url": _url(client, project), "design": d,
            "stage": "mesh", "message": "Model built, meshing…"}


def _url(client, project):
    try:
        return client.get_url(project)
    except Exception:
        return None


def poll(project_id: str, design: dict | None = None) -> dict:
    a = availability()
    if not a["available"]:
        return a
    try:
        client = _client()
        project = client.get_project(project_id)
        mesh, sim = project.get_meshes()[0], project.get_simulations()[0]
        url = _url(client, project)
        J = allsolve.Job
        ms = mesh.get_status()
        if ms in (J.ERROR, J.ABORTED, J.FAILING):
            return {"status": "error", "url": url, "message": f"Meshing failed: {mesh.get_status_reason() or ms}"}
        if ms != J.SUCCESS:
            return {"status": "running", "stage": "mesh", "url": url, "message": "Meshing…"}
        ss = sim.get_status()
        if ss in (None, J.NOT_STARTED):
            sim.start()
            return {"status": "running", "stage": "solve", "url": url, "message": "Solving the flow…"}
        if ss in (J.ERROR, J.ABORTED, J.FAILING):
            return {"status": "error", "url": url, "message": f"Simulation failed: {sim.get_status_reason() or ss}"}
        if ss not in (J.SUCCESS, J.PARTIAL_SUCCESS):
            return {"status": "running", "stage": "solve", "url": url, "message": "Solving the flow…"}
        d = _design(design or {})
        grid = flow_grid(sim, d)
        try:
            vals = sim.get_output_values()
            grid["dp"] = round(float(list(vals.values())[0]["dp"][0]), 2)
        except Exception:
            pass
        logs = "\n".join(map(str, sim.get_logs()))
        m = re.search(r"converged after (\d+) iterations", logs)
        grid["converged"], grid["iterations"] = bool(m), int(m.group(1)) if m else None
        # Never show a solution that did not converge or is physically impossible (Newton can diverge at high Re).
        if not grid["converged"] or grid["u_max"] > 5 * d["v_in"]:
            return {"status": "error", "url": url, "design": d,
                    "message": "Navier–Stokes did not converge for these inputs, so no result is shown. "
                               "Lower the inlet velocity or use a gentler bend (larger Rin)."}
        return {"status": "done", "url": url, "grid": grid, "design": d, "message": "Done."}
    except Exception as e:
        return _err("Fetching the result failed", e)


# ---------------- field -> grid ----------------
def read_field_points(path: str):
    import h5py
    import numpy as np

    raw = open(path, "rb").read()
    if raw[:4] == b"\x28\xb5\x2f\xfd":  # zstd frame
        import zstandard
        raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    with h5py.File(io.BytesIO(raw), "r") as f:
        root = f["VTKHDF"]
        xyz = np.asarray(root["Points"])
        pd = root["PointData"]
        val = np.asarray(pd[list(pd.keys())[0]]).reshape(len(xyz), -1)[:, 0]
    scale = 1e3 if np.abs(xyz[:, :2]).max() < 1 else 1.0  # -> mm
    return xyz[:, 0] * scale, xyz[:, 1] * scale, val


def inside(x, y, d):
    """True where (x, y) [mm] lies inside the U channel."""
    import numpy as np

    r_in, r_out = d["r_in_mm"], d["r_in_mm"] + d["w_mm"]
    legs = (y >= 0) & (y <= LEG * 1e3) & (np.abs(x) >= r_in) & (np.abs(x) <= r_out)
    r = np.hypot(x, y)
    bend = (y < 0) & (r >= r_in) & (r <= r_out)
    return legs | bend


def _download(sim, name):
    out = tempfile.mkdtemp(prefix="cellshield-")
    sim.save_output_field(name=name, output_dir=out, refresh=True)
    f = [p for p in glob.glob(os.path.join(out, "**", "*"), recursive=True) if os.path.isfile(p)][0]
    return read_field_points(f)


def _bin(x, y, val, gx, gy, mask):
    """Mean of node values per grid cell; empty cells inside the channel are filled from neighbours."""
    import numpy as np

    step = gx[1] - gx[0]
    ix = np.clip(np.rint((x - gx[0]) / step).astype(int), 0, len(gx) - 1)
    iy = np.clip(np.rint((y - gy[0]) / step).astype(int), 0, len(gy) - 1)
    acc, cnt = np.zeros((len(gy), len(gx))), np.zeros((len(gy), len(gx)))
    np.add.at(acc, (iy, ix), val)
    np.add.at(cnt, (iy, ix), 1)
    Z = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    for _ in range(30):
        hole = mask & np.isnan(Z)
        if not hole.any():
            break
        P = np.pad(Z, 1, constant_values=np.nan)
        nb = np.stack([P[:-2, 1:-1], P[2:, 1:-1], P[1:-1, :-2], P[1:-1, 2:]])
        with np.errstate(all="ignore"):
            Z = np.where(hole, np.nanmean(nb, axis=0), Z)
    return np.where(mask, Z, np.nan)


def _components(dead):
    """4-connected components of a boolean grid, largest first: list of (cells_iy, cells_ix)."""
    import numpy as np

    seen = np.zeros_like(dead, dtype=bool)
    comps = []
    for j0, i0 in zip(*np.nonzero(dead)):
        if seen[j0, i0]:
            continue
        stack, cells = [(j0, i0)], []
        seen[j0, i0] = True
        while stack:
            j, i = stack.pop()
            cells.append((j, i))
            for jj, ii in ((j + 1, i), (j - 1, i), (j, i + 1), (j, i - 1)):
                if 0 <= jj < dead.shape[0] and 0 <= ii < dead.shape[1] and dead[jj, ii] and not seen[jj, ii]:
                    seen[jj, ii] = True
                    stack.append((jj, ii))
        comps.append(cells)
    comps.sort(key=len, reverse=True)
    return comps


def _where(cx, cy, d):
    """Plain-language location of a point (mm) in the U channel."""
    import math

    r_in, w = d["r_in_mm"], d["w_mm"]
    mid = r_in + w / 2
    if cy < 0:
        return "in the bend, " + ("inner side" if math.hypot(cx, cy) < mid else "outer side")
    side = "inner wall" if abs(cx) < mid else "outer wall"
    if cx > 0:
        return f"after the bend, {side}, {cy:.1f} mm past the bend"
    return f"before the bend, {side}, {cy:.1f} mm before the bend"


def flow_grid(sim, d: dict) -> dict:
    """Velocity on a regular grid + the dead zone: reverse flow, or slow air away from the walls."""
    import numpy as np

    x, y, vx = _download(sim, "vx")
    _, _, vy = _download(sim, "vy")
    r_in, w = d["r_in_mm"], d["w_mm"]
    r_out = r_in + w
    step = w / 14
    gx = np.arange(-r_out, r_out + step / 2, step)
    gy = np.arange(-r_out, LEG * 1e3 + step / 2, step)
    X, Y = np.meshgrid(gx, gy)
    mask = inside(X, Y, d)
    VX, VY = _bin(x, y, vx, gx, gy, mask), _bin(x, y, vy, gx, gy, mask)
    speed = np.hypot(VX, VY)

    # local flow direction of the U: down the inlet leg, around the bend, up the outlet leg
    R = np.maximum(np.hypot(X, Y), 1e-9)
    tx = np.where(Y < 0, -Y / R, 0.0)
    ty = np.where(Y < 0, X / R, np.where(X < 0, -1.0, 1.0))
    along = VX * tx + VY * ty
    wall_dist = np.where(Y < 0, np.minimum(R - r_in, r_out - R), np.minimum(np.abs(X) - r_in, r_out - np.abs(X)))
    with np.errstate(invalid="ignore"):
        backflow = along < -BACKFLOW_TOL * d["v_in"]
        slow_core = (speed < U_SLOW) & (wall_dist > WALL_LAYER * w)
        dead = mask & (backflow | slow_core)

    n_in = max(1, int(np.count_nonzero(mask)))
    zones = []
    for cells in _components(dead)[:3]:
        jj, ii = np.array(cells).T
        area = len(cells) * step * step
        if area < 0.002 * n_in * step * step:  # ignore specks (< 0.2 % of the channel)
            continue
        cx, cy = float(gx[ii].mean()), float(gy[jj].mean())
        zones.append({"x": round(cx, 3), "y": round(cy, 3), "area_mm2": round(area, 4),
                      "backflow": bool(backflow[jj, ii].any()), "where": _where(cx, cy, d)})
    rnd = lambda Z: [[None if np.isnan(v) else round(float(v), 3) for v in row] for row in Z]  # noqa: E731
    return {"x": np.round(gx, 4).tolist(), "y": np.round(gy, 4).tolist(), "u": rnd(speed),
            "dead": [[1 if v else None for v in row] for row in dead],
            "dead_pct": round(100.0 * np.count_nonzero(dead) / n_in, 2), "zones": zones,
            "u_max": round(float(np.nanmax(np.hypot(vx, vy))), 3), "n_nodes": int(len(vx))}


if __name__ == "__main__":  # local test: python api/_allsolve_pipeline.py [project_id [out.json]]
    import json
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:
        pass
    if len(sys.argv) > 1:
        r = poll(sys.argv[1])
        if len(sys.argv) > 2 and r.get("status") == "done":
            json.dump(r, open(sys.argv[2], "w"))
        if "grid" in r:
            g = r.pop("grid")
            r.update(u_max=g["u_max"], dead_pct=g["dead_pct"], zones=g["zones"], dp=g.get("dp"),
                     converged=g.get("converged"), iterations=g.get("iterations"))
    else:
        r = submit({})
    print(json.dumps(r, indent=2))
