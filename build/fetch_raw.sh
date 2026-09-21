#!/bin/sh
# Fetches the source data into build/raw. About 100 MB. curl needs -k on this machine.
mkdir -p "$(dirname "$0")/raw" && cd "$(dirname "$0")/raw" || exit 1
for c in af ar as au eu gr na sa si; do
  curl -s -k -O "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_${c}_lev08_v1c.zip"
done
for n in rivers_lake_centerlines lakes geography_marine_polys; do
  curl -s -k -L -O "https://naciscdn.org/naturalearth/10m/physical/ne_10m_${n}.zip"
done
for z in *.zip; do unzip -o -q "$z" -d "${z%.zip}"; done
