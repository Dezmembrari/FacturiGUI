import cv2
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"

# Map the required assets to the exact screenshot they appear in
ASSETS_TO_CROP = {
    "Intrari.png": "Screenshot 2026-10-12 145321.png",
    "Facturi.png": "Screenshot 2026-10-12 145347.png",
    "Interne.png": "Screenshot 2026-10-12 145321.png",
    "Bonuri_consum.png": "Screenshot 2026-10-12 145420.png",
    "btn_adaug_general.png": "Screenshot 2026-10-12 145448.png",
    "anchor_furnizor.png": "Screenshot 2026-10-12 145514.png",
    "anchor_numar.png": "Screenshot 2026-10-12 145514.png",
    "anchor_data.png": "Screenshot 2026-10-12 145514.png",
    "btn_plus_grid.png": "Screenshot 2026-10-12 145514.png",
    "title_articole.png": "Screenshot 2026-10-12 145541.png",
    "icon_lupa_search.png": "Screenshot 2026-10-12 145541.png",
    "anchor_denumire_nou.png": "Screenshot 2026-10-12 145604.png",
    "anchor_cod_extern.png": "Screenshot 2026-10-12 145604.png"
}

def main():
    ASSETS_DIR.mkdir(exist_ok=True)
    print("=== WinMentor Offline Template Cropper ===")
    print("Drag a box around the requested element. Press SPACE or ENTER to confirm and move to the next.")
    
    for asset_name, screenshot_name in ASSETS_TO_CROP.items():
        img_path = SCREENSHOTS_DIR / screenshot_name
        if not img_path.exists():
            print(f"Skipping {asset_name}: Screenshot {screenshot_name} not found.")
            continue
            
        img = cv2.imread(str(img_path))
        if img is None:
            continue
            
        # Select ROI
        window_title = f"Crop {asset_name} (Press SPACE to save)"
        roi = cv2.selectROI(window_title, img, showCrosshair=True, fromCenter=False)
        cv2.destroyWindow(window_title)
        
        # If a box was drawn (width and height > 0)
        if roi[2] > 0 and roi[3] > 0:
            x, y, w, h = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])
            cropped = img[y:y+h, x:x+w]
            cv2.imwrite(str(ASSETS_DIR / asset_name), cropped)
            print(f"✅ Saved {asset_name}")
        else:
            print(f"⏭️ Skipped {asset_name}")

if __name__ == "__main__":
    main()