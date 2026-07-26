"""Dashboard routes: aggregated metrics + action buttons.

- GET  /api/v1/dashboard/metrics     dashboard metrics, now backed by the
                                      persistent BuildingSimulator (instead
                                      of independent random draws)
- POST /api/v1/simulation/run        triggers a mock EnergyPlus run, persisted
- POST /api/v1/agents/run-cycle      triggers a mock LangGraph agent cycle,
                                      persisted (AgentCycle + linked steps)
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agents.graph import run_mock_cycle
from app.database import get_db
from app.mcp.tools import get_mcp_client
from app.models import AgentCycle, BuildingTick, LLMReasoning, OptimizationMetric, Simulation
from app.schemas.dashboard import SimulationRunRequest
from app.services.building_simulator import get_building_simulator

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

_LAST_SIMULATION_AT: str | None = None


def _persist_tick(db: Session, tick) -> BuildingTick:
    row = BuildingTick(
        sim_time=tick.sim_time,
        occupancy=tick.occupancy,
        outdoor_temp_c=tick.outdoor_temp_c,
        indoor_temp_c=tick.indoor_temp_c,
        humidity_pct=tick.humidity_pct,
        weather_condition=tick.weather_condition,
        solar_radiation_wm2=tick.solar_radiation_wm2,
        hvac_mode=tick.hvac_mode,
        hvac_status=tick.hvac_status,
        hvac_load_kw=tick.hvac_load_kw,
        lighting_load_kw=tick.lighting_load_kw,
        equipment_load_kw=tick.equipment_load_kw,
        total_energy_kw=tick.total_energy_kw,
        comfort_score=tick.comfort_score,
        carbon_kg=tick.carbon_kg,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _series_from_ticks(ticks: list[BuildingTick]) -> list[dict]:
    return [{"time": t.sim_time.strftime("%H:%M"), "value": t.total_energy_kw} for t in ticks]


@router.get("/dashboard/metrics")
def dashboard_metrics(db: Session = Depends(get_db)) -> dict:
    sim = get_building_simulator()
    tick = sim.current()
    _persist_tick(db, tick)

    recent = (
        db.query(BuildingTick)
        .order_by(BuildingTick.sim_time.desc())
        .limit(24)
        .all()
    )
    recent = list(reversed(recent))
    energy_series = _series_from_ticks(recent) or [{"time": tick.sim_time.strftime("%H:%M"), "value": tick.total_energy_kw}]
    temperature_series = [
        {"time": t.sim_time.strftime("%H:%M"), "indoor": t.indoor_temp_c, "outdoor": t.outdoor_temp_c}
        for t in recent
    ] or [{"time": tick.sim_time.strftime("%H:%M"), "indoor": tick.indoor_temp_c, "outdoor": tick.outdoor_temp_c}]

    last_cycle = db.query(AgentCycle).order_by(AgentCycle.id.desc()).first()
    now = datetime.now(timezone.utc)

    return {
        "timestamp": now.isoformat(),
        "energy_usage_kw": tick.total_energy_kw,
        "energy_series": energy_series,
        "hvac_status": tick.hvac_status,
        "hvac_mode": tick.hvac_mode,
        "indoor_temp_c": tick.indoor_temp_c,
        "outdoor_temp_c": tick.outdoor_temp_c,
        "temperature_series": temperature_series,
        "comfort_score": int(tick.comfort_score),
        "carbon_kg_today": tick.daily_carbon_kg,
        "cost_usd_today": round(tick.daily_carbon_kg / 0.42 * 0.15, 2),
        "ai_decision": last_cycle.decision if last_cycle else "No agent cycle has run yet.",
        "ai_confidence": last_cycle.confidence if last_cycle else 0.0,
        "optimization_status": "active" if last_cycle else "monitoring",
        "simulation_status": "idle" if _LAST_SIMULATION_AT else "never_run",
        "last_simulation_at": _LAST_SIMULATION_AT,
    }


@router.post("/simulation/run")
def simulation_run(payload: SimulationRunRequest, db: Session = Depends(get_db)) -> dict:
    global _LAST_SIMULATION_AT
    mcp = get_mcp_client()
    result = mcp.run_simulation(payload.idf_path, payload.epw_path, payload.is_baseline)

    # An explicit "Run Simulation" also advances the persistent building
    # clock by one tick so its effect is visible on the live pages.
    tick = get_building_simulator().force_tick()
    _persist_tick(db, tick)

    sim = Simulation(
        simulation_id=result["simulation_id"],
        status=result["status"],
        idf_path=payload.idf_path,
        epw_path=payload.epw_path,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=result["duration_seconds"],
        output_path=result["output_path"],
        is_baseline=payload.is_baseline,
    )
    db.add(sim)
    db.commit()
    db.refresh(sim)

    m = result["metrics"]
    metric = OptimizationMetric(
        simulation_id=sim.id,
        timestamp=datetime.now(timezone.utc),
        total_energy_kwh=m["total_energy_kwh"],
        hvac_energy_kwh=m["hvac_energy_kwh"],
        lighting_energy_kwh=m["lighting_energy_kwh"],
        cooling_energy_kwh=m["cooling_energy_kwh"],
        heating_energy_kwh=m["heating_energy_kwh"],
        peak_demand_kw=m["peak_demand_kw"],
        cost_usd=m["cost_usd"],
        carbon_kg=m["carbon_kg"],
        comfort_pmv=m["comfort_pmv"],
        comfort_ppd=m["comfort_ppd"],
    )
    db.add(metric)
    db.commit()

    _LAST_SIMULATION_AT = datetime.now(timezone.utc).isoformat()
    return result


@router.post("/agents/run-cycle")
def agents_run_cycle(db: Session = Depends(get_db)) -> dict:
    result = run_mock_cycle()
    building_tick = result.pop("building_tick")
    _persist_tick(db, building_tick)

    cycle = AgentCycle(
        cycle_id=result["cycle_id"],
        timestamp=datetime.now(timezone.utc),
        status=result["status"],
        decision=result["decision"],
        confidence=result["confidence"],
        duration_ms=result["duration_ms"],
        tools_used=json.dumps(result["tools_used"]),
        generated_actions=json.dumps(result["generated_actions"]),
        validation_result=json.dumps(result["validation_result"]),
    )
    db.add(cycle)
    db.commit()
    db.refresh(cycle)

    for step in result["steps"]:
        db.add(LLMReasoning(
            cycle_id=cycle.id,
            agent_name=step["agent"],
            timestamp=datetime.now(timezone.utc),
            reasoning=step.get("detail", ""),
            planned_actions=json.dumps([step["action"]]),
            tool_calls=json.dumps([step["action"]]),
            confidence=step.get("confidence", result["confidence"]),
            latency_ms=step.get("latency_ms"),
        ))
    db.commit()

    return result
