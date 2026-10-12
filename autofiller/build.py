import shutil
import subprocess
from pathlib import Path

def main():
    base_dir = Path(__file__).parent
    deploy_dir = base_dir / "WinMentor_AutoFiller"

    print("🔨 Step 1: Compiling WinMentor Agent to .exe...")
    # Run PyInstaller silently
    subprocess.run(
        ["pyinstaller", "--onefile", "winmentor_agent.py"], 
        check=True,
        cwd=base_dir
    )

    print("📁 Step 2: Assembling final deployment folder...")
    
    # Wipe the old deployment folder if it exists
    if deploy_dir.exists():
        shutil.rmtree(deploy_dir)
    deploy_dir.mkdir()

    # Move the newly compiled .exe into the deployment folder
    exe_src = base_dir / "dist" / "winmentor_agent.exe"
    exe_dst = deploy_dir / "winmentor_agent.exe"
    shutil.move(str(exe_src), str(exe_dst))

    # Copy the assets folder into the deployment folder
    assets_src = base_dir / "assets"
    assets_dst = deploy_dir / "assets"
    shutil.copytree(str(assets_src), str(assets_dst))

    # Clean up PyInstaller junk folders
    shutil.rmtree(base_dir / "build", ignore_errors=True)
    shutil.rmtree(base_dir / "dist", ignore_errors=True)
    spec_file = base_dir / "winmentor_agent.spec"
    if spec_file.exists():
        spec_file.unlink()

    print(f"\n✅ Build complete! ")
    print(f"You can now copy the entire '{deploy_dir.name}' folder directly to the operator's Desktop.")

if __name__ == "__main__":
    main()