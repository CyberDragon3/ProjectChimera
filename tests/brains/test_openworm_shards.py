from pathlib import Path

from chimera.brains.openworm_shards import bundle_summary, default_graphs_dir, neuron_names_from_graphs


def test_neuron_names_from_graphs_reads_distinct_names(tmp_path: Path):
    graphs_dir = tmp_path / ".owm" / "bundles" / "openworm%2Fc_elegans" / "2" / "graphs"
    graphs_dir.mkdir(parents=True)
    shard = graphs_dir / "sample.nt"
    shard.write_text(
        '<http://data.openworm.org/sci/bio/Neuron#AIAL> '
        '<http://schema.openworm.org/2020/07/sci/bio/Cell/name> "AIAL" .\n'
        '<http://data.openworm.org/sci/bio/Neuron#AIAL> '
        '<http://schema.openworm.org/2020/07/sci/bio/Neuron/type> "interneuron" .\n'
        '<http://data.openworm.org/sci/bio/Neuron#RMED> '
        '<http://schema.openworm.org/2020/07/sci/bio/Cell/name> "RMED" .\n'
        '<http://data.openworm.org/sci/bio/Neuron#RMED> '
        '<http://schema.openworm.org/2020/07/sci/bio/Cell/name> "RMED" .\n',
        encoding="utf-8",
    )

    assert neuron_names_from_graphs(graphs_dir) == ["AIAL", "RMED"]


def test_default_graphs_dir_prefers_encoded_bundle_path(tmp_path: Path):
    graphs_dir = tmp_path / ".owm" / "bundles" / "openworm%2Fc_elegans" / "2" / "graphs"
    graphs_dir.mkdir(parents=True)

    assert default_graphs_dir(tmp_path) == graphs_dir


def test_bundle_summary_reports_count_and_sample(tmp_path: Path):
    graphs_dir = tmp_path / ".owm" / "bundles" / "openworm" / "c_elegans" / "2" / "graphs"
    graphs_dir.mkdir(parents=True)
    for name in ("AVAL", "AVAR", "PVCL"):
        (graphs_dir / f"{name}.nt").write_text(
            f'<http://data.openworm.org/sci/bio/Neuron#{name}> '
            '<http://schema.openworm.org/2020/07/sci/bio/Cell/name> '
            f'"{name}" .\n',
            encoding="utf-8",
        )

    summary = bundle_summary(tmp_path)

    assert summary["present"] is True
    assert summary["neuron_count"] == 3
    assert summary["sample"] == ["AVAL", "AVAR", "PVCL"]
