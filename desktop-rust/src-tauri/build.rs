fn main() {
    let reader = tauri_build::AppManifest::new().commands(&[
        "choose_library",
        "refresh_library",
        "load_run",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(reader))
        .expect("prepare reader assets and command permissions");
}
