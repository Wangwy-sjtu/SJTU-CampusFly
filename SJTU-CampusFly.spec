from pathlib import Path

project = Path(SPECPATH)
a = Analysis(
    [str(project / "qtui.py")], pathex=[str(project)], binaries=[],
    datas=[(str(project / "assets"), "assets"),
           (str(project / "data" / "tencent_road_graph.candidate.json"), "data"),
           (str(project / "THIRD_PARTY_NOTICES.md"), "."),
           (str(project / "licenses"), "licenses")],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "IPython"], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], name="SJTU校园飞", console=False,
          exclude_binaries=True, contents_directory=".",
          icon=str(project / "assets" / "campusfly.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name="SJTU-CampusFly")
