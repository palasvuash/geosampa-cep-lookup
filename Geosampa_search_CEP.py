import pandas as pd
import streamlit as st

from Automatic_Geosampa_Search import lookup

st.set_page_config(page_title="GeoSampa CEP Lookup", page_icon="📍", layout="centered")
st.title("📍 GeoSampa CEP Lookup")
st.caption("Zoning (ZER check) and CET department for a São Paulo address")


@st.cache_data(show_spinner=False, ttl=60 * 60 * 24)
def cached_lookup(cep, number):
    return lookup(cep, number)


with st.form("search"):
    c1, c2 = st.columns([2, 1])
    cep = c1.text_input("CEP", placeholder="05449-010")
    number = c2.text_input("House number (optional)", placeholder="147")
    submitted = st.form_submit_button("Search", type="primary")

if submitted:
    digits = "".join(ch for ch in cep if ch.isdigit())
    if len(digits) != 8:
        st.error("A CEP has 8 digits.")
        st.stop()

    try:
        with st.spinner("Geocoding and querying GeoSampa..."):
            res = cached_lookup(digits, number.strip() or None)
    except Exception as e:
        st.error(f"Lookup failed: {e}")
        st.stop()

    # ---- Verdict ----
    zones = res["zones_found"]
    if res["is_zer_exact"]:
        st.success(f"**In a ZER**: {', '.join(zones)}")
    elif res["zer_nearby"]:
        st.warning(
            f"**ZER nearby, not confirmed.** Zones found within "
            f"{res['search_radius_m']} m: {', '.join(zones)}. "
            "The point is probably on a street or plaza beside the ZER. "
            "Confirm on GeoSampa with the exact lot."
        )
    elif zones:
        st.info(f"**Not a ZER.** Zone: {', '.join(zones)}")
    else:
        st.error("No zoning polygon found near this point.")

    # ---- Key facts ----
    m1, m2 = st.columns(2)
    m1.metric("Zone", res["zone"] or " / ".join(zones) or "n/a")
    m2.metric("CET department", res["cet_departamento"] or
              " / ".join(res["cet_departamentos_found"]) or "n/a")

    st.write(f"**Address:** {res['address']}")
    st.caption(f"Zoning law: {res['zoning_law'] or 'n/a'}")

    # ---- Geocoding quality warnings ----
    if res["geocoder"] == "nominatim":
        st.caption(f"Geocoded by OpenStreetMap as: {res['nominatim_match']}")
        if res["house_number_used"] and res["house_number_used"] not in (res["nominatim_match"] or ""):
            st.warning(
                "OpenStreetMap matched the street but not the house number, so the point "
                "is somewhere along the street and the number had no effect."
            )

    # ---- Map ----
    st.map(pd.DataFrame({"lat": [res["lat"]], "lon": [res["lon"]]}), zoom=16)

    with st.expander("Raw result"):
        st.json(res)
    