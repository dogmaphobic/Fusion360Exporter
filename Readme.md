This is a Fusion 360 Script to bulk export your files. Can export:

* `f3d` files to `f3d`, `igs`, `stp`, `smt`, `sat`, `3mf` and `stl`
* `f2d` files to `pdf`
* can export drawings to `dxf`

# Installation

1) Download this repo and unzip it somewhere.
2) In Fusion, goto UTILITIES > ADD-INS > Scripts and Add-Ins (or just hit Shift+S)
   * UTILITIES was previously known as TOOLS
3) Next to "My Scripts", hit the green plus icon
4) Select the folder where you unzipped it
5) "Exporter" should now appear under "My Scripts"

or see the [offical docs](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-install-an-ADD-IN-and-Script-in-Fusion-360.html) though they recommend copying the folder into the scripts directory and restarting which seems more complicated to me.

# Versions

Released versions are tagged with `YYYYMMDD.N` and can be found in the [tags page](https://github.com/aconz2/Fusion360Exporter/tags)

# Usage

1) Goto Scripts and Add-Ins
2) Select Exporter from "My Scripts"
3) Hit Run. It will take a second to display the options panel as it is fetching your list of projects.
4) After selecting your options and hitting okay, **your computer will be unusable**. The script will be potentially opening and exporting a lot of documents and each time it opens one, Fusion likes to make itself the active window which means you can't just have this run in the background (as far as I know).

# Options

1) Directory: This defaults to a folder called Fusion360Exports on your desktop
2) File types: Select the export file types you want for each file. See [File Types](#FileTypes)
3) Download Open Folder: Uses the current folder in the Data Panel as the starting folder to initiate an export. See [Projects/Folders](#ProjectsFolders)
4) Projects: Select the projects (or folders) you want to operate on
    * Show Project Folders: Instead of selecting whole projects, you can select specific folders. See [Projects/Folders](#ProjectsFolders)
5) Unhide All: When checked, it will unhide all components and all bodies (recursively) so that the exported files contain all bodies
6) Export Sketches as DXF: Each sketch will get exported as dxf
7) Versions: Control how many versions are exported. See [Versions](#Versions)
8) Version Separator Is Space: Controls which character `_` or ` ` (space) is used between the name and version (ie. `name_v42.stl` or `name v42.stl`). Defaults to the original `_` and checking this will use ` ` (space) to better match Fusion.
9) Export Non-Design Files: If true, all [non-design files](https://help.autodesk.com/view/PLM/ENU/?guid=UG-ATTTAB-ATTACHMENTS) will be exported. Note that only the latest version will be exported and the version number will not be appended.
10) Retry Previously Interrupted Models: A model that was active when Fusion last stopped unexpectedly is skipped by default. Enable this to try quarantined models again.
11) Minimum Free Memory: Stop before starting another model if available system memory falls below this many GiB or 10% of physical RAM, whichever is greater. The default is 4 GiB.

The last run's settings are loaded by default (if they exist). They are stored next to the `Exporter.py` file on your file system in a file called `last_settings.json`. In "My Scripts", you can right-click "Exporter" and then "Open file location" to get there. If you rename projects or folders you will have to reselect those projects.

# File Types

To export `f2d` files as `pdf` (this is the only available option; they cannot be downloaded as f2d), select `PDF` for the export type.

All other file types apply to `f3d`.

# Projects/Folders

If `Download Open Folder` is selected, the files will be saved with all parent folders created to match the structure in Fusion. For example, if `A/B/C` is the active folder, then we export everything in and beneath folder `C` and those get saved on your computer as `<export directory>/A/B/C/<file name etc>`

By default, selecting a project from `Export Projects` will go through every file in every folder recursively.

If you enable `Show Project Folders`, the `Export Projects` dropdown is populated with the top level folders (with an additional `<root>`) of each project. Selecting the `<root>` folder visits files in the project's root folder, but does not recurse. Selecting any other folder will visit all the files in that folder AND recurse into it.

# Versions

By default, only the latest version of each file will be exported. You can change this behavior to either
1) save all versions
2) save the previous `n` versions. (`n=0` corresponds to the default because `0` additional versions are saved)

# Operation

For each document in each selected project, it checks for a non-empty file named `<export directory>/<project name>/<document name><version separator><version name>.<file extension>` or that file name with one of the supported compressed/archived suffixes. An output is skipped only when its modification time is at least as new as the cloud document. Missing, empty, or stale outputs are exported. If there are multiple formats to export, the document is opened only once. The exported file's `Date Modified` attribute (or `mtime`) is set to the cloud document's modified time.

Exports are first written to a hidden partial file in the destination directory. The completed, non-empty partial is atomically moved over the final path, so a failed export does not destroy a known-good local file or masquerade as a completed backup.

For sketches, it will create a folder hiearchy like `<export directory>/<project name>/<component names ...>/<sketch name>.dxf`.

Since document names might have invalid filename characters, we attempt to replace them with spaces. In order to avoid a false collision, if any chars are replaced, the document name will have 8 hexchars of sha256 hash of the original utf-8 encoded document name. Eg `model 1/2 \ * ? <morechars> ||` would be saved as `model 1 2        morechars    _29a6fecc_v1.f3d`

In some ways this is an export and in others it is a sync: it does not re-export current files, and it skips opening documents it does not need (with the caveat that exporting sketches requires opening each design).

It creates a log file at `<export_directory>/<timestamp>.txt`. A progress dialog reports the current model and memory status. Its Stop button requests a clean stop; if Fusion is inside a blocking API call, the request takes effect as soon as that call returns and the current document can be closed.

The exporter also maintains `<export_directory>/.fusion360-export-state.json`. Before opening a file version it records that version durably, and clears the record after the model is closed. If Fusion itself crashes, is killed, or cannot close the document, the next run moves the interrupted version into quarantine and continues past it. On the first run after upgrading from a pre-journal version, a timestamped exporter log whose final line is an `Opening ... vN` entry seeds the same quarantine. The retry option is explicit because retrying a model that crashes Fusion may crash it again.

The completion dialog counts processed model versions separately from output files. `Saved` and `Skipped current` are per output format, so their total can be greater than the number of processed versions. `Errors` is also per failed output and includes quarantined model versions; consult the log for the corresponding model names and Fusion exceptions.

# File Time

Starting in version `20240813.1`, newly exported files have their `Date Modified` (or `mtime`) attribute set to the document's modified time. The exporter now uses that timestamp to decide whether an existing output is current. If you do not want exported timestamps changed, replace `set_mtime` with a no-op, but freshness checking will then reflect the filesystem timestamps instead of the cloud timestamps.

Folders' `mtime` are not handled.

# Limitations + Known Issues

1) Not sure what other file types are out there (simulation data maybe? etc) but it only handles `.f3d` documents
2) Only visible bodies are included in exports to all file formats except `f3d`. Use the "Unhide All" option to unhide them before exporting
3) Image renders might cause an error. See [#4](https://github.com/aconz2/Fusion360Exporter/issues/4)
4) Cloud solves might cause an error. See [#3](https://github.com/aconz2/Fusion360Exporter/issues/3)
5) Python exceptions returned by Fusion are logged and skipped. A native Fusion crash or a permanently blocked cloud call cannot be caught by a Python script; the durable in-progress journal is what lets the next run identify and skip the responsible model.

# Saved Settings

To easily run the same settings repeatedly, you can copy-paste the `Template` folder in `UserScripts` so that you have `UserScripts/YourScriptName/YourScriptName.{py,manifest}`. Then, open up a log file of a run that you want to replicate and copy paste the JSON blob at the beginning into `YourScriptName.py`. Then add this into Fusion as a script and run normally.

Note that we store project and folder id's, so renaming a project/folder will not break your backup script. But if you happen to replace the folder with a new one of the same name, it won't work.

You might run into an issue with the `VERSION_SEPARATOR` (whether it is export `file_v42.stl` or `file v42.stl`) if you are using saved settings.

# TODO (Maybe)

1) Saving electronics documents? these are `fbrd` files
2) Per-component export of `stl` (or other format); currently only the root component is exported with everything in it

# Credit

* Pulled the addition of `3mf` from [tavdog](https://github.com/tavdog/Fusion360Exporter)
* Problematic `"` in project names reported by TheShanMan
* Installation doc improvement reported by sqlBender
* Version ordering bug reported and diagnosed by loglow
* Non-design file support added by [raphael-bmec-co](https://github.com/raphael-bmec-co)
* f2d support and using active folder by robertkuyper
* bug fix by hiroloquy
* Figured out f3d thumbnails with Happy1Snappy

# Dev

Discussion about some changes are in issue [23](https://github.com/aconz2/Fusion360Exporter/issues/23) on folder output structure. I implemented most of those ideas in the `dev` branch some time ago -- including a way to write tests -- but never felt like it was a great change so it is abandoned.

The reliability policy is isolated in `resilience.py` and has no Autodesk imports. Run the test suite outside Fusion with:

```sh
python3 test.py
```
