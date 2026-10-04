# OpenStreetMap background snapshot

**© OpenStreetMap contributors**

The database in `background.gpkg` is derived from OpenStreetMap and is made available under the [Open Data Commons Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).

The source and attribution requirements are described on the [official OpenStreetMap copyright page](https://www.openstreetmap.org/copyright). Keep this notice and the license link when redistributing this data. Maps using the data must provide OpenStreetMap attribution and identify the ODbL license. Modified versions of the database are subject to the ODbL share-alike terms.

This snapshot covers a 400 m square around the provisional Gangnam Station center. Processing projected the features to EPSG:5179, clipped them to the square, classified the retained road, park, land-use and water geometries, and excluded building features. Original OSM identifiers and available names and selected tags are preserved. Road surfaces, widths and missing names were not inferred.

`background.provenance.json` records the source endpoint, OSM database timestamp, retrieval time, exact bounds, feature counts, processing steps and file checksum.

This notice applies to the OSM background database. Separately sourced building data and the software have their own notices.
