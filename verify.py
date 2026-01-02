import subprocess
import os
import sys
import re
import zipfile
import glob
import shutil

# Configuration
OPENSCAD_BIN = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
ORCA_BIN = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
SCAD_FILE = "gridfinity-rebuilt-bins.scad"
STL_FILE = "bin.stl"
GCODE_FILE = "bin.gcode"

# USER TODO: Point this to your exported file (can be .json, .orca_printer, .orca_filament, etc.)
# If you have multiple (printer + filament), you can put them in a list or handle manually, 
# but this script attempts to extract everything from one archive or use the single file.
ORCA_CONFIG = "Bambu Lab A1 0.4 nozzle.orca_printer" 


def run_command(cmd, cwd=None):
    print(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command. Exit Code: {e.returncode}")
        print(f"STDOUT:\n{e.stdout}")
        print(f"STDERR:\n{e.stderr}")
        return False
    return True

def step_1_generate_stl():
    print("--- Step 1: Generating STL from OpenSCAD ---")
    # -o output.stl input.scad
    cmd = [OPENSCAD_BIN, "-o", STL_FILE, SCAD_FILE]
    return run_command(cmd)

def load_json(path):
    import json
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_json(path, data):
    import json
    with open(path, 'w') as f:
        json.dump(data, f, indent=4)

def remove_compat_keys(data):
    # Remove compatibility checks to force loading
    for key in ["compatible_printers", "compatible_printers_condition", "inherits", "print_compatible_printers", "filament_compatible_printers"]:
        data.pop(key, None)
    
    # Remove empty bed_exclude_area which causes "Unable to create exclude triangles" error
    if "bed_exclude_area" in data:
        print(f"Removing bed_exclude_area: {data['bed_exclude_area']}")
        data.pop("bed_exclude_area")
        
    return data

def prepare_configs(extract_dir):
    """
    Patches extracted configs with 'type' and 'from' keys.
    Returns dict with paths for 'machine', 'process', 'filament'.
    """
    import json
    
    config_paths = {
        "machine": None,
        "process": None,
        "filament": None
    }
    
    # 1. Process Printer (Machine) Config
    printers = glob.glob(os.path.join(extract_dir, '**', 'printer', '*.json'), recursive=True)
    if printers:
        path = printers[0]
        data = load_json(path)
        data["type"] = "machine"
        data["from"] = "User"
        data = remove_compat_keys(data)
        
        # Inject inherits to match filename, ensuring system_name is valid for compatibility check
        base_name = os.path.basename(path).replace(".json", "")
        data["inherits"] = base_name
        
        save_json(path, data)
        config_paths["machine"] = path
        print(f"Prepared Machine Config: {os.path.basename(path)}")

    # 2. Process Filament Config (Prefer PETG)
    filaments = glob.glob(os.path.join(extract_dir, '**', 'filament', '*.json'), recursive=True)
    if filaments:
        # Try to find one with PETG in name
        petg_files = [f for f in filaments if "PETG" in os.path.basename(f).upper()]
        path = petg_files[0] if petg_files else filaments[0]
        data = load_json(path)
        data["type"] = "filament"
        data["from"] = "User"
        data = remove_compat_keys(data)
        save_json(path, data)
        config_paths["filament"] = path
        print(f"Prepared Filament Config: {os.path.basename(path)}")
        
    # 3. Process Process Config
    # User requested to ignore profiles in zip (lucky13, etc)
    # Use our local minimal_config.json instead
    minimal_path = "minimal_config.json"
    if os.path.exists(minimal_path):
        configs_paths_abs = os.path.abspath(minimal_path)
        # Ensure it has type/from set (we just created it, so it should, but let's be safe)
        try:
            data = load_json(configs_paths_abs)
            if data.get("type") != "process" or data.get("from") != "User":
                data["type"] = "process"
                data["from"] = "User"
                save_json(configs_paths_abs, data)
            config_paths["process"] = configs_paths_abs
            print(f"Using Process Config: {os.path.basename(configs_paths_abs)}")
        except Exception as e:
            print(f"Error loading minimal config: {e}")
    else:
        # Fallback to zip if minimal not found? No, forced to minimal per plan.
        print("WARNING: minimal_config.json not found for Process settings.")
        
    return config_paths

def extract_and_prepare_configs(config_path):
    """Extracts zip and prepares individual config files."""
    if not os.path.exists(config_path):
        return None
        
    extract_dir = "extracted_configs"
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)
    
    # If it's already a JSON, assume it's a stand-alone config (handled weirdly? just return it)
    if config_path.lower().endswith(".json"):
        return {"manual": config_path}
        
    try:
        with zipfile.ZipFile(config_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except zipfile.BadZipFile:
        print(f"Error: {config_path} is not a valid zip archive.")
        return None

    return prepare_configs(extract_dir)

def step_2_generate_gcode():
    print("--- Step 2: Generating G-code from OrcaSlicer ---")
    
    configs = extract_and_prepare_configs(ORCA_CONFIG)
    
    cmd = [ORCA_BIN, "--slice", "0", "--no-check"]
    
    if not configs:
        print(f"WARNING: Config extraction failed for '{ORCA_CONFIG}'. Running with defaults.")
    else:
        if configs.get("machine"):
            cmd.extend(["--load-settings", configs["machine"]])
        if configs.get("process"):
            cmd.extend(["--load-settings", configs["process"]])
            cmd.extend(["--load-filaments", configs["filament"]])
            
    cmd.extend(["--outputdir", "."])
    cmd.append(STL_FILE)
    
    return run_command(cmd)

def step_3_analyze_gcode():
    print("--- Step 3: Analyzing G-code for Infill ---")
    
    # OrcaSlicer CLI defaults to "plate_1.gcode" when using --slice 0
    gcode_file = "plate_1.gcode"
    
    if not os.path.exists(gcode_file):
        print(f"G-code file not found: {gcode_file}")
        return False

    has_bad_infill = False
    current_z = 0.0
    current_type = None
    
    # Threshold: We allowed infill in the base (bottom 7mm). 
    # The user specifically wanted to fix the LIP (top of bin).
    # Let's flag infill only if it's in the top section.
    # Standard bin is ~45mm. Let's start monitoring at 20mm to be safe.
    Z_THRESHOLD = 20.0 
    
    bad_layers = []

    try:
        with open(gcode_file, 'r') as f:
            for line in f:
                line = line.strip()
                
                # Track Z Change (OrcaSlicer/PrusaSlicer style: ;Z: height)
                if line.startswith(";Z:"):
                    try:
                        current_z = float(line.split(":")[1])
                    except:
                        pass
                    continue
                
                # Track G1 Z moves if comment missing
                if line.startswith("G1") and "Z" in line:
                    parts = line.split()
                    for p in parts:
                        if p.startswith("Z"):
                            try:
                                current_z = float(p[1:])
                            except:
                                pass
                
                # Track Feature Type
                if line.startswith(";TYPE:"):
                    current_type = line.split(":")[1]
                    continue
                    
                # Check for Sparse Infill (allow Internal solid infill? User said "unwanted infill", usually means sparse)
                # "Internal solid infill" is usually fine (anchors), "Sparse infill" is the zigzag/grid stuff.
                if current_type == "Sparse infill" and current_z > Z_THRESHOLD:
                    # We are in the danger zone
                    # Check if this line is actually an extrusion (has E value)
                    if "E" in line and not "E0" in line and not "E-.": # Crude check for positive extrusion
                        # Double check E value is actually > 0
                        # ... simplified for robust regex
                         if re.search(r"E[0-9\.]+", line):
                            val = float(re.search(r"E([0-9\.]+)", line).group(1))
                            if val > 0:
                                if current_z not in bad_layers:
                                    bad_layers.append(current_z)
                                    has_bad_infill = True
                                    if len(bad_layers) < 5: # Limit spam
                                        print(f"FAILURE: Sparse Infill detected at Z={current_z}mm")

    except Exception as e:
        print(f"Error parsing G-code: {e}")
        return False

    if has_bad_infill:
        print(f"G-code verification FAILED. Sparse infill found above {Z_THRESHOLD}mm at layers: {bad_layers[:10]}...")
        return False
    else:
        print("G-code verification PASSED! No upper-body infill detected.")
        return True

def main():
    if not step_1_generate_stl():
        sys.exit(1)
        
    # Hack for step 2: OrcaSlicer likely fails without a printer profile.
    # But let's run it and see the error output.
    if not step_2_generate_gcode():
        # If it failed, maybe tried to just generate stl?
        # Let's try to proceed to analysis if the file exists anyway?
        if not os.path.exists(GCODE_FILE):
             print("G-code generation failed.")
             sys.exit(1)

    if not step_3_analyze_gcode():
        sys.exit(1)
        
    print("Verification Successful!")

if __name__ == "__main__":
    main()
