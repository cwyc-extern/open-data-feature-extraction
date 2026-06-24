Power Line Downloads
====================

Data folder layout:

  data/power-grid-infrastructure/   Power lines, transformers, substations (this README)
  data/telecommunications/        AfTerFibre + ITU BBmaps + OSM fibre links
  data/OFDS-schema/               OFDS 0.3.0 conversions for QGIS (see below)

Vector outputs are GeoPackage (.gpkg) files under data/power-grid-infrastructure/.
Filenames include a snapshot timestamp (YYYYMMDDTHHMMSS, local time) indicating
when the dataset was downloaded and is valid as an OSM / portal snapshot.


OpenStreetMap downloads
-----------------------

Script: download_osm_powerlines.py
Data source: Overpass API (ODbL — https://www.openstreetmap.org/copyright)

By default each run downloads all three:
  - Power lines (power=line), filtered by regional transmission voltages
  - Electricity transformers (power=transformer nodes), comparable to the UN
    GeoPortal OSM transformer layers
  - Electrical substations (power=substation), filtered by operational voltage

Reference (Tunisia transformers): Tunisia Electricity Transformers (OSM) 2021
https://geoportal.un.org/arcgis/home/item.html?id=f5069ff368e34aed988a03c5f0d8effd

Power lines
~~~~~~~~~~~

Country  Code  Regional voltages (kV)  Power lines  Valid as of              Output file                                      Size
-------  ----  ----------------------  -----------  -----------------------  -----------------------------------------------  --------
Tunisia  TN    150, 225                         308  2026-06-12 11:28:28      power-grid-infrastructure/powerlines_tn_150_225kv_20260612T112828.gpkg     400 KiB
Morocco  MA    150, 225                         559  2026-06-12 10:54:46      power-grid-infrastructure/powerlines_ma_150_225kv_20260612T105446.gpkg     692 KiB
Indonesia ID   150, 225                       3,301  2026-06-12 10:57:04      power-grid-infrastructure/powerlines_id_150_225kv_20260612T105704.gpkg     2.1 MiB
Egypt    EG    132, 220, 400                    860  2026-06-12 11:01:59      power-grid-infrastructure/powerlines_eg_132_220_400kv_20260612T110159.gpkg 996 KiB
Vietnam  VN    132, 220, 400                  1,371  2026-06-12 11:03:17      power-grid-infrastructure/powerlines_vn_132_220_400kv_20260612T110317.gpkg 876 KiB
India    IN    132, 220, 400                 29,982  2026-06-12 11:05:39      power-grid-infrastructure/powerlines_in_132_220_400kv_20260612T110539.gpkg 25.9 MiB
Bangladesh BD  132, 220, 400                    525  2026-06-12 11:06:18      power-grid-infrastructure/powerlines_bd_132_220_400kv_20260612T110618.gpkg 628 KiB

Electricity transformers (nodes)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Country  Code  OSM filter          Transformers  Valid as of              Output file                          Size
-------  ----  ------------------  ------------  -----------------------  -----------------------------------  --------
Tunisia  TN    power=transformer            245  2026-06-12 11:32:01      power-grid-infrastructure/transformers_tn_20260612T113201.gpkg  124 KiB
Morocco  MA    power=transformer            130  2026-06-12 11:32:41      power-grid-infrastructure/transformers_ma_20260612T113241.gpkg 116 KiB
Indonesia ID   power=transformer            955  2026-06-12 11:34:05      power-grid-infrastructure/transformers_id_20260612T113405.gpkg 220 KiB
Egypt    EG    power=transformer            142  2026-06-12 11:34:30      power-grid-infrastructure/transformers_eg_20260612T113430.gpkg 116 KiB
Vietnam  VN    power=transformer            985  2026-06-12 11:36:27      power-grid-infrastructure/transformers_vn_20260612T113627.gpkg 212 KiB
India    IN    power=transformer          5,718  2026-06-12 11:37:35      power-grid-infrastructure/transformers_in_20260612T113735.gpkg 760 KiB
Bangladesh BD  power=transformer             97  2026-06-12 11:37:54      power-grid-infrastructure/transformers_bd_20260612T113754.gpkg 112 KiB

Transformer attribute columns:
  osm_id, power, name, operator, voltage, voltage_high, voltage_low, transformer,
  substation, geometry

Electrical substations
~~~~~~~~~~~~~~~~~~~~~~

Country  Code  Regional voltages (kV)  Substations  Valid as of              Output file                                        Size
-------  ----  ----------------------  -----------  -----------------------  -------------------------------------------------  --------
Tunisia  TN    150, 225                          31  2026-06-12 12:11:17      power-grid-infrastructure/substations_tn_150_225kv_20260612T121028.gpkg      108 KiB
Morocco  MA    150, 225                          92  2026-06-12 12:11:36      power-grid-infrastructure/substations_ma_150_225kv_20260612T121120.gpkg      120 KiB
Indonesia ID   150, 225                         907  2026-06-12 12:12:58      power-grid-infrastructure/substations_id_150_225kv_20260612T121143.gpkg    252 KiB
Egypt    EG    132, 220, 400                    176  2026-06-12 12:13:13      power-grid-infrastructure/substations_eg_132_220_400kv_20260612T121301.gpkg 124 KiB
Vietnam  VN    132, 220, 400                    251  2026-06-12 12:14:26      power-grid-infrastructure/substations_vn_132_220_400kv_20260612T121316.gpkg 140 KiB
India    IN    132, 220, 400                  5,201  2026-06-12 12:14:57      power-grid-infrastructure/substations_in_132_220_400kv_20260612T121429.gpkg 804 KiB
Bangladesh BD  132, 220, 400                    158  2026-06-12 12:15:59      power-grid-infrastructure/substations_bd_132_220_400kv_20260612T121500.gpkg 124 KiB

Substation attribute columns:
  osm_id, osm_type, power, name, operator, voltage, voltage_primary,
  voltage_secondary, substation, geometry

Notes
~~~~~

- Tunisia, Morocco, and Indonesia use the 150 / 225 kV filter (common in North
  Africa and parts of Southeast Asia in OSM tagging).
- Egypt, Vietnam, India, and Bangladesh use the broader 132 / 220 / 400 kV
  filter; the 150 / 225 kV filter returned no matching lines in OSM for those
  countries.
- India is the largest OSM line dataset by far (~30,000 lines, ~26 MiB).
- Transformers are not voltage-filtered; all OSM nodes tagged power=transformer
  within the country boundary are included. The UN Tunisia reference layer
  contained 31 features (OSM snapshot 2021); current OSM has more mapped nodes.
- Substations use the same regional voltage filters as power lines. Point
  locations are OSM nodes or way centroids (polygon substations). Voltage match
  checks voltage, voltage:primary, voltage:secondary, voltage-high, and
  voltage-low tags after download.
- Valid as of = Overpass query / download time (live OSM snapshot).
- Use --lines-only, --transformers-only, or --substations-only for one feature type.

Re-run examples:
  python download_osm_powerlines.py --country TN --voltages 150,225 --yes
  python download_osm_powerlines.py --country IN --voltages 132,220,400 --substations-only --yes


UN GeoPortal downloads
----------------------

Script: download_un_geoportal_powerlines.py
Portal item: Tunisia Electricity Transmissions Lines (WorldBank) 2020
Portal URL: https://geoportal.un.org/arcgis/home/item.html?id=6c53909d572744b8912dde97664683ed

Metadata
~~~~~~~~

Field                Value
-------------------  --------------------------------------------------------------------------
Item ID              6c53909d572744b8912dde97664683ed
Item name            ElectricityLines_TUN_new
Title                Tunisia Electricity Transmissions Lines (WorldBank) 2020
Type                 ArcGIS Feature Service (hosted)
Feature service URL  https://pro-ags2.dfs.un.org/arcgis/rest/services/Hosted/ElectricityLines_TUN_new/FeatureServer
Layer                0 — DerivedElectricityTransmissionDistributionLines_TUN_WorldBank_2020
Geometry type        Polyline (EPSG:4326)
Country              Tunisia
Source year          2020
Underlying source    World Bank — Derived Map Of Global Electricity Transmission And Distribution Lines
                     (https://datacatalog.worldbank.org/search/dataset/0038055)
Method / tool        gridfinder (https://github.com/carderne/gridfinder)
                     Night-time lights + roads/OSM routing; HV (>70 kV) and MV (10–70 kV) lines
License              Creative Commons Attribution 4.0 (CC BY 4.0)
SDG tags             SDG:7, SDG:9
Portal last modified 2026-02-26

Summary
~~~~~~~

Country  Code  Regional voltages (kV)  Power lines  Valid as of              Output file                                                Size
-------  ----  ----------------------  -----------  -----------------------  ---------------------------------------------------------  --------
Tunisia  TN    HV (>70), MV (10–70)*         6,754  2026-06-12 11:21:07      power-grid-infrastructure/un_geoportal_electricitylines_tun_new_20260612T112107.gpkg 1.9 MiB

* This dataset does not provide per-line kV values. Voltage classes are described
  in the source metadata as high voltage (>70 kV) and medium voltage (10–70 kV).
  Underlying grid data is from 2020; Valid as of is the portal download snapshot.

Attribute columns saved:
  objectid, country, length_km, source_year, mainsource, SHAPE__Length, geometry

Repeated dataset-level HTML fields (concepts, sources, methods, limitations,
licenses) are documented here and omitted from the output file by default.
Use --keep-metadata-columns to retain them on every feature.

Re-run example:
  python download_un_geoportal_powerlines.py --yes


AfTerFibre downloads
--------------------

Script: download_afterfibre.py
Project: AfTerFibre — African Terrestrial Fibre Optic Cable Mapping
Homepage: https://afterfibre.nsrc.org/
Maintainer: Network Startup Resource Center (NSRC)
License: Open data (CC-BY-4.0 per EU Africa Knowledge Platform listing)

Metadata
~~~~~~~~

Field                Value
-------------------  --------------------------------------------------------------------------
Dataset              africa-fiber (vector layer: fiber)
TileJSON URL         https://d316kar6yg8hyq.cloudfront.net/africa-fiber.json
Vector tile URL      https://d316kar6yg8hyq.cloudfront.net/africa-fiber/{z}/{x}/{y}.mvt
Coverage bounds      W -17.42, S -34.50, E 55.74, N 36.87 (EPSG:4326)
Download method      Reconstructed from NSRC MapLibre vector tiles (zoom 10)
Legacy API           https://afterfibre.carto.com (af_fibrephase — no longer available)
Related repo         https://github.com/stevesong/nsrc-afterfibre
EU reference         https://africa-knowledge-platform.ec.europa.eu/dataset/terrestrial-fibre-status

Attribute columns:
  cartodb_id, country, iso2, operator, operator_name, owner, owner_name,
  phase_name, technology, type, live, go_live, fibre_cores, source_url,
  contributor, contrib_email, operator_web_url, owner_web_url, created_at,
  updated_at, geometry

Summary
~~~~~~~

Combined file: telecommunications/afterfibre_20260612T122625/afterfibre_africa_20260612T122625.gpkg
Total segments: 144 across 40 countries | Valid as of: 2026-06-12 12:35:17 | Size: 180 KiB

Country                         Code  Segments  Output file
------------------------------  ----  --------  ----------------------------------------------------------
Angola                          AO           3  afterfibre_ao_20260612T122625.gpkg
Burkina Faso                    BF           7  afterfibre_bf_20260612T122625.gpkg
Burundi                         BI           1  afterfibre_bi_20260612T122625.gpkg
Benin                           BJ           2  afterfibre_bj_20260612T122625.gpkg
Botswana                        BW           2  afterfibre_bw_20260612T122625.gpkg
DR Congo                        CD           3  afterfibre_cd_20260612T122625.gpkg
Republic of the Congo           CG           2  afterfibre_cg_20260612T122625.gpkg
Ivory Coast                     CI           4  afterfibre_ci_20260612T122625.gpkg
Cameroon                        CM           4  afterfibre_cm_20260612T122625.gpkg
Algeria                         DZ           1  afterfibre_dz_20260612T122625.gpkg
Egypt                           EG           2  afterfibre_eg_20260612T122625.gpkg
Ethiopia                        ET           3  afterfibre_et_20260612T122625.gpkg
Gabon                           GA           2  afterfibre_ga_20260612T122625.gpkg
Ghana                           GH          17  afterfibre_gh_20260612T122625.gpkg
Gambia                          GM           3  afterfibre_gm_20260612T122625.gpkg
Guinea                          GN           1  afterfibre_gn_20260612T122625.gpkg
Kenya                           KE          11  afterfibre_ke_20260612T122625.gpkg
Libya                           LY           3  afterfibre_ly_20260612T122625.gpkg
Morocco                         MA           1  afterfibre_ma_20260612T122625.gpkg
Madagascar                      MG           2  afterfibre_mg_20260612T122625.gpkg
Mali                            ML           5  afterfibre_ml_20260612T122625.gpkg
Mauritania                      MR           3  afterfibre_mr_20260612T122625.gpkg
Malawi                          MW           3  afterfibre_mw_20260612T122625.gpkg
Mozambique                      MZ           2  afterfibre_mz_20260612T122625.gpkg
Namibia                         NA           2  afterfibre_na_20260612T122625.gpkg
Niger                           NE           1  afterfibre_ne_20260612T122625.gpkg
Nigeria                         NG          14  afterfibre_ng_20260612T122625.gpkg
Réunion                         RE           2  afterfibre_re_20260612T122625.gpkg
Rwanda                          RW           1  afterfibre_rw_20260612T122625.gpkg
Sudan                           SD           1  afterfibre_sd_20260612T122625.gpkg
Sierra Leone                    SL           1  afterfibre_sl_20260612T122625.gpkg
Senegal                         SN           3  afterfibre_sn_20260612T122625.gpkg
Somalia                         SO           1  afterfibre_so_20260612T122625.gpkg
Chad                            TD           3  afterfibre_td_20260612T122625.gpkg
Togo                            TG           2  afterfibre_tg_20260612T122625.gpkg
Tanzania                        TZ           3  afterfibre_tz_20260612T122625.gpkg
Uganda                          UG           9  afterfibre_ug_20260612T122625.gpkg
South Africa                    ZA           7  afterfibre_za_20260612T122625.gpkg
Zambia                          ZM           3  afterfibre_zm_20260612T122625.gpkg
Zimbabwe                        ZW           3  afterfibre_zw_20260612T122625.gpkg

All country files live under telecommunications/afterfibre_20260612T122625/.

Notes
~~~~~

- The legacy Carto SQL export (af_fibrephase) now redirects to a dead endpoint;
  this script downloads the current NSRC-hosted vector tiles instead.
- Use --zoom to trade detail for download time (default 10; max tile zoom 14).
- Outputs include one combined Africa file plus per-country GeoPackages.

Re-run example:
  python download_afterfibre.py --yes


ITU BBmaps downloads
--------------------

Script: download_itu_bbmaps.py
Source: ITU Interactive Transmission Maps (BBmaps)
Portal: https://www.itu.int/en/ITU-D/Technology/Pages/InteractiveTransmissionMaps.aspx
GeoCatalogue: https://bbmaps.itu.int/
GeoNetwork record: https://bbmaps.itu.int/geonetwork/srv/api/records/f9af598b-da16-4a7a-a757-6cffc02e9565
License: ITU open geodata (see portal terms)

Metadata
~~~~~~~~

Field                Value
-------------------  --------------------------------------------------------------------------
WFS endpoint         https://bbmaps.itu.int/geoserver/itu-geocatalogue/wfs
Layer                itu-geocatalogue:trx_geocatalogue
Geometry type        LineString (EPSG:4326)
Technologies         Terrestrial fibre and microwave links (operational, planned, etc.)
Country assignment   Natural Earth 110m admin boundaries (longest intersection)
Download method      GeoServer WFS 2.0 (single request; paginated with sortBy=uid if needed)

Attribute columns:
  uid, id, type_inf, status, type_, country_name, iso2, geometry

type_inf values include Fibre Operational, Microwave Operational, Fibre Planned,
Fibre Under Construction, Fibre Proposed, and related status variants.

Summary
~~~~~~~

Combined file: telecommunications/itu_bbmaps_20260612T124341/itu_bbmaps_global_20260612T124341.gpkg
WFS features: 40,358 | Assigned segments: 40,358 | Countries: 156
Valid as of: 2026-06-12 12:43:41 | Size: 7.8 MiB

Country (top 20 by segment count)     Code  Segments
------------------------------------  ----  --------
India                                 IN       2,242
United States of America              US       2,195
Brazil                                BR       1,514
Mexico                                MX       1,313
Unknown                               —        1,152
Australia                             AU       1,106
Italy                                 IT       1,077
Argentina                             AR       1,053
Bangladesh                            BD       1,028
Russia                                RU         951
Pakistan                              PK         875
Philippines                           PH         829
Germany                               DE         789
United Kingdom                        GB         760
Nepal                                 NP         726
Nigeria                               NG         695
France                                —          681
South Africa                          ZA         669
Myanmar                               MM         668
Indonesia                             ID         609

All country files live under telecommunications/itu_bbmaps_20260612T124341/.

Notes
~~~~~

- The WFS layer has no country field; countries are inferred spatially and may
  differ slightly from ITU's internal catalogue grouping.
- Segments crossing borders are assigned to the country with the longest shared
  geometry; ~1,100 segments could not be matched to a country polygon.
- Use --technology fibre|microwave to filter by type_inf before country split.
- Natural Earth ISO_A2 is missing for some territories (e.g. France shows as —).

Re-run example:
  python download_itu_bbmaps.py --yes
  python download_itu_bbmaps.py --technology fibre --yes


OSM fibre backbone downloads
----------------------------

Script: download_osm_fibre_backbone.py
Data source: Overpass API (ODbL — https://www.openstreetmap.org/copyright)

Tag guidance (verified June 2026):
  communication=line  — wiki-recommended primary tag (~15k ways globally)
  telecom=line        — secondary / less common tag (~3.7k ways globally)
  telecom:medium=fibre — combine with communication=line for terrestrial fibre

Wiki: https://wiki.openstreetmap.org/wiki/Tag:communication=line

Summary
~~~~~~~

Country    Code  OSM filter (both tags)  Routes  Valid as of              Output file                                        Size
---------  ----  ----------------------  ------  -----------------------  -------------------------------------------------  --------
Tunisia    TN    communication + telecom      3  2026-06-12 13:33:31      telecommunications/osm_fibre_tn_20260612T133331.gpkg   104 KiB
Morocco    MA    communication + telecom      2  2026-06-12 13:37:07      telecommunications/osm_fibre_ma_20260612T133707.gpkg   104 KiB
Egypt      EG    communication + telecom      8  2026-06-12 13:37:18      telecommunications/osm_fibre_eg_20260612T133718.gpkg   108 KiB
Indonesia  ID    communication + telecom     21  2026-06-12 13:40:47      telecommunications/osm_fibre_id_20260612T134047.gpkg   128 KiB
Vietnam    VN    communication + telecom      2  2026-06-12 13:41:32      telecommunications/osm_fibre_vn_20260612T134132.gpkg    96 KiB
India      IN    communication + telecom      9  2026-06-12 13:42:25      telecommunications/osm_fibre_in_20260612T134225.gpkg   100 KiB
Bangladesh BD    communication + telecom      0  2026-06-12 13:42:59      (no features in OSM)

Coverage is sparse in OSM for this tag set; most mapped routes are submarine
cables (location=underwater). Bangladesh has no communication=line or
telecom=line ways. Use --terrestrial-only or --fibre-only to narrow.

Attribute columns:
  osm_id, primary_tag, communication, telecom, telecom_medium, location, name,
  operator, capacity, cables, ref, submarine, seamark_type, seamark_category,
  geometry

Re-run examples:
  python download_osm_fibre_backbone.py --country TN --yes
  for cc in MA EG ID VN IN BD; do python download_osm_fibre_backbone.py --country $cc --yes; done
  python download_osm_fibre_backbone.py --country IN --fibre-only --terrestrial-only --yes


OFDS schema conversion
----------------------

Script: convert_to_ofds.py
Standard: Open Fibre Data Standard (OFDS) 0.3.0
Reference: https://standard.ofds.info/en/0.3/reference/publication_formats/geojson.html

Converts GeoPackages under data/power-grid-infrastructure/ and
data/telecommunications/ into OFDS network packages and GeoJSON layers under
data/OFDS-schema/, preserving the same subfolder structure.

Per source file outputs:
  <basename>_package.json   OFDS network package (networks array)
  <basename>_spans.geojson  LineString spans (power lines, fibre routes)
  <basename>_nodes.geojson    Point nodes (substations, transformers)

Mapping (for QGIS / smart-grid planning):
  Power lines, fibre routes, ITU/AfTerFibre segments  -> OFDS spans
  Substations, transformers                         -> OFDS nodes
  Domain-specific source fields                     -> x_* extension properties
    (e.g. x_voltage, x_infrastructureDomain, x_sourceSchema)

Default country filter: TN, MA, EG, ID, VN, IN, BD (project countries of interest).

Latest conversion (2026-06-12):
  45 source files -> 53,138 spans, 15,088 nodes

Re-run examples:
  python convert_to_ofds.py
  python convert_to_ofds.py --countries all --force
  python convert_to_ofds.py --countries TN,EG --force

QGIS: add *_spans.geojson and *_nodes.geojson layers from data/OFDS-schema/.


Environment
-----------

Conda environment: odp-map-setup
  conda activate odp-map-setup

Dependencies: geopandas, requests, tqdm, mapbox-vector-tile, libcoveofds (see requirements.txt)
