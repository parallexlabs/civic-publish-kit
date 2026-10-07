# Live read-only rehearsal evidence

The narrowed six-command CLI was run against the public City of Surrey Development Applications layer 167 on 2026-10-06 (Pacific time). This is extraction and local review evidence. City GIS publishes reviewed data through its own ArcGIS Online; no City credentials, hosted-layer writes, tenancy testing or publication were performed.

Command: `uv run civic-publish extract --config config/rehearsal.json --out _build/cli-live-snapshot`.

Snapshot start UTC: `2026-10-07T00:27:12Z`. Origin: live; synthetic: false; scope: complete service, `where=1=1`. Initial and final counts: **13,880 / 13,880**. All 13,880 unique OBJECTIDs were reconciled against the initial and final ID lists. Seven native pages contain 2000, 2000, 2000, 2000, 2000, 2000 and 1880 records. The advertised 2000-record limit did not truncate the export silently. OBJECTID is completeness evidence; PROJECT_NO is the business identity.

Metadata declared native EPSG:26910, polygon geometry, maxRecordCount 2000 and ordered pagination support. The extractor also retrieved the first page in EPSG:4326 for paired transformation verification. Count and ID rereads reduce concurrent-edit risk; the service offers no snapshot isolation and unchanged counts do not prove unchanged attributes.

Snapshot manifest SHA256: `49df6a9465515b0ab7034c36a7f2405300f39823680589eee86abc4b94b8244a`. Original response bytes, exact URLs, UTC request times and file hashes remain in the ignored local snapshot. This document records the portable acquisition audit without committing the full live dataset.

## Exact public requests

| Response file | UTC requested | Bytes | SHA256 | Exact URL |
|---|---|---:|---|---|
| `metadata.json` | `2026-10-07T00:27:12Z` | 5627 | `c28bb2ae70341b77b679dee18f1457758fbbe8f14e784f98299d319a9deffd35` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167?f=json` |
| `count.json` | `2026-10-07T00:27:12Z` | 15 | `5bc27a07903853f33513d41594e18f67cbdd59ec57f14a35fcfddab1db4d573b` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&returnCountOnly=true&f=json` |
| `ids.json` | `2026-10-07T00:27:12Z` | 72220 | `41e0af9415dab9b603b5a58fd4fc3fc4eef23361542b028654df7005b0b40023` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&returnIdsOnly=true&f=json` |
| `pages/page-0001.geojson` | `2026-10-07T00:27:13Z` | 2218381 | `0824f2f75e3b445427949bdeb0dd4f43aff1feef3687ed329f1795308f7bb0d3` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=0&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0002.geojson` | `2026-10-07T00:27:13Z` | 1884973 | `7c5f1d2316db58e9f5b699a9802a46e93c11c61d240131bee61fbcc290b90030` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=2000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0003.geojson` | `2026-10-07T00:27:14Z` | 1699975 | `10170d7430eb2649e30a9661fcc64900285dee36d52e9ebfde075be492c69fdd` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=4000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0004.geojson` | `2026-10-07T00:27:14Z` | 1908867 | `c588d375e47386a6beb3df510afa1b8b02987be89a00129215673b94d594726f` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=6000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0005.geojson` | `2026-10-07T00:27:15Z` | 1730247 | `87a6f29bd358e79a7967033f507bcad5b92aa57ad4f48a9f2b9b14b468a5a96c` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=8000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0006.geojson` | `2026-10-07T00:27:15Z` | 1841653 | `73fec5695d3d6a19494b49f78ea506c706c4fb97ba85816d0610ee5a72461282` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=10000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `pages/page-0007.geojson` | `2026-10-07T00:27:16Z` | 1641407 | `f37271c11aef77f8e3387f359a2581fd9dc7e1af5724b1838825bb74da9a2552` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=12000&resultRecordCount=2000&returnGeometry=true&outSR=26910&f=geojson` |
| `crs-check/page-0001-epsg4326.geojson` | `2026-10-07T00:27:16Z` | 2255489 | `6a0224659c587e6aa5b5446125ae35135fc48e4610f66cd2979a6548c40efb65` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&outFields=OBJECTID%2CPROJECT_NO%2CDESCRIPTION%2CSTATUS&orderByFields=OBJECTID+ASC&resultOffset=0&resultRecordCount=2000&returnGeometry=true&outSR=4326&f=geojson` |
| `final-count.json` | `2026-10-07T00:27:17Z` | 15 | `5bc27a07903853f33513d41594e18f67cbdd59ec57f14a35fcfddab1db4d573b` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&returnCountOnly=true&f=json` |
| `final-ids.json` | `2026-10-07T00:27:17Z` | 72220 | `41e0af9415dab9b603b5a58fd4fc3fc4eef23361542b028654df7005b0b40023` | `https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query?where=1%3D1&returnIdsOnly=true&f=json` |

## Paired genuine CRS benchmark

The committed native/server-WGS84 fixtures preserve genuine original public-service bytes and dated exact query URLs. Native `(513897.3032031245, 5445719.12890625)` transforms with offline pyproj 3.7.2 and `always_xy=True` to `(-122.8093697266551, 49.164130380626034)`. The City server pair is `(-122.8093697266551, 49.16413038062605)`. The manual paired benchmark passes across two genuine features and 42 vertices: maximum server discrepancy is 2.1316282072803006e-14 degrees, and maximum inverse-roundtrip discrepancy is 1.862645149230957e-09 metres (below 1 mm). Automated paired checks and erroneous-transform rejection remain required gates.

## Local review outcome

Actual commands `normalize --config config/rehearsal.json --snapshot _build/cli-live-snapshot --out _build/cli-live-normalized` and `validate --input _build/cli-live-normalized --out _build/cli-live-report` both returned exit2 and wrote review files. The bundle contains 13,854 valid project rows and no building rows. Validation reports 15 blocking findings: 11 duplicate PROJECT_NO values (22 features) and four invalid native geometries. There are 15,613 warnings: 2,709 missing descriptions preserved as nullable titles, 12,902 unrecognized statuses preserved as unknown, missing source last-edit time, and no supplied buildings. `package` returned exit 2 and did not create a store. These files cannot be published; the original source data was not repaired or silently approved.

## Fixture rehearsal disclosure

Frozen fixture A is the genuine bounded query `OBJECTID IN (1,2,3)`; B is `OBJECTID IN (1,2)`. The 3-to-2 change rehearses a deliberately narrower query scope, not observed deletion of a City record. They are samples, not complete-service exports. No building rows are invented from application polygons. The independent repeat of A produced identical snapshot, normalized, report and all 17 package-file bytes at a fixed timestamp. After the source/store corrections, all six commands passed in fresh fixture output directories. Candidate creation left current unchanged; A was explicitly acknowledged, B warned about the 3-to-2 scope removal, stale acknowledgment returned exit 2 with current bytes unchanged, a fresh B acknowledgment promoted locally, and rollback selected A with all 17 prior package-file hashes identical. A=`r-76c05710e37ac5f042bb4a080b3027e5`, B=`r-81c2ae19f0a6a6242ad25df586280eb0`. Both use the fixed package timestamp 2026-10-06T23:17:20Z. The subsequent 880-case offline suite passes with 86.54% global branch coverage. Wheel/sdist build and the author-written six-command demo pass. The stronger rollback safeguard rejects a never-acknowledged deletion candidate with exit 2 and leaves the current pointer byte-identical; a fresh explicitly reviewed A-to-B rehearsal still restores all 17 original A package files byte for byte. Full-repository review is recorded in STATUS.md at delivery.

## Blocking source findings

| Stable project ID | Blocking finding |
|---|---|
| `13-0269-00` | PROJECT_NO '13-0269-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `15-0031-00` | PROJECT_NO '15-0031-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `16-0074-00` | PROJECT_NO '16-0074-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `18-0024-00` | PROJECT_NO '18-0024-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `18-0076-00` | PROJECT_NO '18-0076-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `25-0255-00` | PROJECT_NO '25-0255-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `7825-0002-00` | PROJECT_NO '7825-0002-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `92-0547-00` | PROJECT_NO '92-0547-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `93-0368-00` | PROJECT_NO '93-0368-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `94-0182-00` | PROJECT_NO '94-0182-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `96-0093-00` | PROJECT_NO '96-0093-00' appears 2 times. Projects need a unique stable ID, and OBJECTID is never used instead. |
| `16-0640-00` | Project 16-0640-00: the native geometry is invalid (Ring Self-intersection[515481.778671874 5433753.12679688]); it is never repaired. |
| `16-0343-00` | Project 16-0343-00: the native geometry is invalid (Ring Self-intersection[514368.25140625 5449072.15929688]); it is never repaired. |
| `19-0113-00` | Project 19-0113-00: the native geometry is invalid (Ring Self-intersection[508592.3134375 5445216.66320313]); it is never repaired. |
| `01-0025-00` | Project 01-0025-00: the native geometry is invalid (Ring Self-intersection[508594.213437501 5445215.67320313]); it is never repaired. |

## Verification after final review repairs

The repaired application was checked again on October6 at20:00PT against the same dated live snapshot. Normalize/validate still correctly block publication with13854 project rows,0 buildings,15 blocking findings and15613 warnings; package returns exit2 and creates no release store. The mandatory complete paired CRS check succeeds for the real first page. The four review defects (missing/partial pair, unterminated quoted CSV, contradictory source counts, and retry byte accounting) have local before/after reproduction receipts.

The six-command genuine-fixture rehearsal again passes with fresh acknowledgments and all17 prior package files byte-identical after rollback. Final artifact IDs after the CSV-import guidance was added to the embedded runbook and data dictionary are A=`r-5144c581a3d96dc348dadf04735521ca` and B=`r-348d2bb621f17950ca4951e0ed4964d7`; changed documentation bytes correctly produce new package identities. Two fresh stores using the CLI default source-retrieval timestamp produce identical A IDs and all17 file hashes. The saved local demonstration and report are `_build/delivery-final-demo/` (ignored, reproducible via scripts/demo.sh). Final source/test gates and review disposition are recorded in STATUS.md at delivery.

The final CLI/report replay again returned exit2 for the same 15 live blockers and created no store. Two independent stores using the default source timestamp matched all17 package hashes. The final demo requires exit2 and the exact stale-acknowledgment diagnostic, and checks that current pointer bytes remain unchanged. These checks use the dated captured source, with no additional City request.
