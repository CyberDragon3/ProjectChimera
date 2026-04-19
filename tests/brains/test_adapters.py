from chimera.brains import BRAINS_AVAILABLE, summary
from chimera.brains.bmtk_adapter import BmtkAdapter
from chimera.brains.flygym_adapter import FlygymAdapter
from chimera.brains.owmeta_adapter import OwmetaAdapter


def test_brains_available_shape():
    assert set(BRAINS_AVAILABLE.keys()) == {"psutil", "owmeta", "bmtk", "flygym"}
    # psutil is a hard dep — always True
    assert BRAINS_AVAILABLE["psutil"] is True
    # others are booleans whatever their value
    for k in ("owmeta", "bmtk", "flygym"):
        assert isinstance(BRAINS_AVAILABLE[k], bool)


def test_summary_shape():
    s = summary()
    assert set(s.keys()) == {"owmeta", "bmtk", "flygym"}
    for key, info in s.items():
        assert info["framework"] == key
        assert isinstance(info["available"], bool)
        assert "role" in info


def test_owmeta_adapter_reports_302_when_available():
    a = OwmetaAdapter()
    # test is robust to absence: neuron_count is None iff not available
    if a.available:
        assert a.neuron_count() == 302
    else:
        assert a.neuron_count() is None


def test_bmtk_adapter_default_glif3_shape():
    a = BmtkAdapter()
    d = a.allen_default_glif3()
    assert set(d.keys()) == {"tau_m_ms", "v_rest_mv", "v_reset_mv", "v_thresh_mv", "refractory_ms"}
    # Reasonable ranges
    assert 5.0 < d["tau_m_ms"] < 100.0
    assert -90.0 < d["v_rest_mv"] < -40.0
    assert d["v_thresh_mv"] > d["v_reset_mv"]


def test_bmtk_glif_params_converts_bmtk_units_to_neuro_cfg():
    # BMTK stores V in volts + tau in seconds; our NeuroCfg uses mV + ms.
    # Adapter multiplies by 1000 to convert.
    bmtk_cell = {"dynamics_params": {"tau_m": 0.020, "V_reset": -0.070, "V_th": -0.050, "t_ref": 0.003}}
    a = BmtkAdapter()
    p = a.glif_params_from_dict(bmtk_cell)
    assert p["tau_m_ms"] == 20.0
    assert p["v_reset_mv"] == -70.0
    assert p["v_thresh_mv"] == -50.0
    assert p["refractory_ms"] == 3.0


def test_flygym_adapter_reports_default_model_names():
    a = FlygymAdapter()
    info = a.info()
    assert info["default_model"] == "NeuroMechFly"
    assert info["default_arena"] == "FlatTerrain"
