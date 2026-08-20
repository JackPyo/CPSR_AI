import io
import json
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from openai import OpenAI

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = APP_DIR / "CPSR_Master_DB_All_INCI_Aliases_for_Colab.xlsx"

st.set_page_config(
    page_title="CPSR AI Screening",
    page_icon="🧪",
    layout="wide",
)

# ---------- Basic styling ----------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem; padding-bottom: 3rem;}
    div[data-testid="stMetric"] {
        border: 1px solid rgba(128,128,128,.22);
        border-radius: 12px;
        padding: 10px 14px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

STATUS_KR = {
    "FAIL": "FAIL",
    "REVIEW": "REVIEW",
    "PASS": "PASS",
    "NO MATCH IN DB": "DB 미매칭",
}

# ---------- Normalization ----------
def norm_text(x):
    if pd.isna(x):
        return ""
    return " ".join(str(x).strip().upper().split())

def norm_cas(x):
    if pd.isna(x):
        return ""
    return str(x).strip().replace(" ", "")

def norm_ec(x):
    if pd.isna(x):
        return ""
    return str(x).strip().replace(" ", "")

def parse_numeric(value):
    if pd.isna(value) or str(value).strip() == "":
        return None
    if isinstance(value, (int, float, np.number)):
        return float(value)
    txt = str(value).replace(",", ".").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?\s*%?", txt):
        return float(txt.replace("%", "").strip())
    return None

def category_matches(rule_value, actual_value):
    rv = norm_text(rule_value)
    av = norm_text(actual_value)
    if rv in ("", "ALL", "NAN"):
        return True
    if not av:
        return False
    if rv == av:
        return True
    if av in rv or rv in av:
        return True
    return False


# ---------- Database ----------
@st.cache_data(show_spinner=False)
def load_db_from_path(path_string):
    path = Path(path_string)
    regulatory = pd.read_excel(path, sheet_name="Regulatory")
    exposure = pd.read_excel(path, sheet_name="Exposure")
    toxicology = pd.read_excel(path, sheet_name="Toxicology")
    mapping = pd.read_excel(path, sheet_name="Product_Mapping")
    return prepare_databases(regulatory, exposure, toxicology, mapping)

@st.cache_data(show_spinner=False)
def load_db_from_bytes(db_bytes):
    bio = io.BytesIO(db_bytes)
    regulatory = pd.read_excel(bio, sheet_name="Regulatory")
    bio.seek(0)
    exposure = pd.read_excel(bio, sheet_name="Exposure")
    bio.seek(0)
    toxicology = pd.read_excel(bio, sheet_name="Toxicology")
    bio.seek(0)
    mapping = pd.read_excel(bio, sheet_name="Product_Mapping")
    return prepare_databases(regulatory, exposure, toxicology, mapping)

def prepare_databases(regulatory, exposure, toxicology, mapping):
    for df in [regulatory, exposure, toxicology, mapping]:
        if "Active" in df.columns:
            df["Active"] = (
                df["Active"].fillna("Y").astype(str).str.upper().str.strip()
            )

    required_reg = ["INCI", "CAS", "EC No.", "Annex", "Rule Type"]
    missing = [c for c in required_reg if c not in regulatory.columns]
    if missing:
        raise ValueError(f"Regulatory 시트 필수 컬럼 누락: {missing}")

    regulatory["INCI_NORM"] = regulatory["INCI"].map(norm_text)
    regulatory["CAS_NORM"] = regulatory["CAS"].map(norm_cas)
    regulatory["EC_NORM"] = regulatory["EC No."].map(norm_ec)

    if "INCI" in toxicology.columns:
        toxicology["INCI_NORM"] = toxicology["INCI"].map(norm_text)
    if "CAS" in toxicology.columns:
        toxicology["CAS_NORM"] = toxicology["CAS"].map(norm_cas)

    return regulatory, exposure, toxicology, mapping


# ---------- Formula ----------
def read_formula(uploaded_file):
    df = pd.read_excel(uploaded_file)
    required = {"INCI", "Concentration (%)"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            "처방 파일에 다음 필수 컬럼이 없습니다: " + ", ".join(sorted(missing))
        )

    for col in ["CAS", "EC No.", "Function"]:
        if col not in df.columns:
            df[col] = ""

    df["INCI"] = df["INCI"].fillna("").astype(str).str.strip()
    df["CAS"] = df["CAS"].fillna("").astype(str).str.strip()
    df["EC No."] = df["EC No."].fillna("").astype(str).str.strip()
    df["Concentration (%)"] = pd.to_numeric(
        df["Concentration (%)"], errors="coerce"
    )
    df = df.dropna(subset=["Concentration (%)"]).copy()
    df = df[df["INCI"].ne("") | df["CAS"].ne("") | df["EC No."].ne("")]
    return df


# ---------- Regulatory rule engine ----------
def find_regulatory_matches(ing, regulatory_db):
    active = regulatory_db[regulatory_db["Active"].eq("Y")].copy()

    inci = norm_text(ing.get("INCI", ""))
    cas = norm_cas(ing.get("CAS", ""))
    ec = norm_ec(ing.get("EC No.", ""))

    matches = []
    seen_rows = set()

    # Exact matching. One regulatory row is returned only once; the strongest
    # successful identifier is recorded in the order INCI -> CAS -> EC.
    if inci:
        for idx, row in active[active["INCI_NORM"].eq(inci)].iterrows():
            if idx not in seen_rows:
                matches.append(("INCI", row))
                seen_rows.add(idx)

    if cas:
        for idx, row in active[active["CAS_NORM"].eq(cas)].iterrows():
            if idx not in seen_rows:
                matches.append(("CAS", row))
                seen_rows.add(idx)

    if ec:
        for idx, row in active[active["EC_NORM"].eq(ec)].iterrows():
            if idx not in seen_rows:
                matches.append(("EC", row))
                seen_rows.add(idx)

    return matches

def assess_rule(ing, rule, reg_product_category, rinse_type):
    annex = norm_text(rule.get("Annex", ""))
    rule_type = norm_text(rule.get("Rule Type", ""))
    concentration = float(ing["Concentration (%)"])

    product_rule = rule.get("Product Category", "")
    lr_rule = rule.get("Leave-on / Rinse-off", "")

    cat_ok = category_matches(product_rule, reg_product_category)
    lr_ok = category_matches(lr_rule, rinse_type)

    max_raw = rule.get("Max Concentration (%)")
    numeric_limit = parse_numeric(rule.get("Numeric Limit (%)"))
    if numeric_limit is None:
        numeric_limit = parse_numeric(max_raw)

    limit_basis = norm_text(rule.get("Limit Basis", ""))
    grouped_key = norm_text(rule.get("Grouped Assessment Key", ""))

    if annex == "II" or rule_type == "PROHIBITED":
        return "FAIL", "Annex II 금지성분과 정확히 일치합니다."

    if grouped_key or limit_basis not in ("", "AS STATED IN ANNEX"):
        if "RETINOL EQUIVALENT" in limit_basis or limit_basis == "RE":
            return (
                "REVIEW",
                "Retinol Equivalent(RE) 기준의 그룹 규제로, 각 Vitamin A 성분을 RE로 환산·합산한 후 별도 검토가 필요합니다.",
            )
        return (
            "REVIEW",
            "그룹 규제 또는 특수 농도 기준이 적용되어 단순 함량 비교만으로 자동 판정할 수 없습니다.",
        )

    if not cat_ok or not lr_ok:
        return (
            "REVIEW",
            "성분은 일치하나, 제품 카테고리 또는 Leave-on/Rinse-off 조건이 명확하게 일치하지 않아 추가 검토가 필요합니다.",
        )

    extra_text = " ".join(
        [
            str(rule.get("Other Conditions", "") or ""),
            str(product_rule or ""),
            str(max_raw or ""),
        ]
    ).upper()

    complex_triggers = [
        "EXCEPT",
        "ONLY",
        "NOT TO BE USED",
        "PH",
        "FREE ACID",
        "FREE BASE",
        "HAIR DYE",
        "OXIDATIVE",
        "NON-OXIDATIVE",
        "WHEN MIXED",
        "READY FOR USE",
        "CHILD",
        "YEARS",
        "PRESENT OR RELEASED",
    ]
    has_complex_text = any(t in extra_text for t in complex_triggers)

    if numeric_limit is not None:
        if concentration > numeric_limit:
            return (
                "FAIL",
                f"함량 {concentration:g}%가 최대 허용농도 {numeric_limit:g}%를 초과합니다.",
            )

        if has_complex_text:
            return (
                "REVIEW",
                "최대 허용농도 기준은 충족하나, 추가 규제 조건에 대한 검토가 필요합니다.",
            )

        return (
            "PASS",
            f"함량 {concentration:g}%가 최대 허용농도 {numeric_limit:g}% 이하로 기준을 충족합니다.",
        )

    if annex in ("III", "IV", "V", "VI"):
        return (
            "REVIEW",
            "해당 Annex 규제 항목과 일치하나, 규제 조건을 단순 수치 기준만으로 자동 판정하기 어려워 추가 검토가 필요합니다.",
        )

    return (
        "REVIEW",
        "규제 DB와 일치하는 항목이 확인되었으나, 규제 적합성에 대한 추가 검토가 필요합니다.",
    )

def run_regulatory_screening(
    formula_df, regulatory_db, reg_product_category, rinse_type
):
    rows = []

    for _, ing in formula_df.iterrows():
        matches = find_regulatory_matches(ing, regulatory_db)

        if not matches:
            rows.append(
                {
                    "INCI": ing.get("INCI", ""),
                    "CAS": ing.get("CAS", ""),
                    "EC No.": ing.get("EC No.", ""),
                    "Concentration (%)": ing.get("Concentration (%)"),
                    "Match By": "",
                    "Annex": "",
                    "Entry No.": "",
                    "Rule Type": "",
                    "Status": "NO MATCH IN DB",
                    "Reason": "현재 Regulatory DB에서 INCI, CAS 또는 EC No.의 정확한 일치 항목을 찾지 못했습니다.",
                    "Product Category Rule": "",
                    "Max Concentration (%)": "",
                    "Numeric Limit (%)": "",
                    "Limit Basis": "",
                    "Grouped Assessment Key": "",
                    "Warning": "",
                    "Other Conditions": "",
                    "Original Chemical Name": "",
                    "INCI Mapping Method": "",
                    "Mapping Confidence": "",
                    "Parent Rule ID": "",
                    "Condition Code": "",
                    "Legal Source": "",
                    "Source URL": "",
                }
            )
            continue

        for match_by, rule in matches:
            status, reason = assess_rule(
                ing, rule, reg_product_category, rinse_type
            )
            rows.append(
                {
                    "INCI": ing.get("INCI", ""),
                    "CAS": ing.get("CAS", ""),
                    "EC No.": ing.get("EC No.", ""),
                    "Concentration (%)": ing.get("Concentration (%)"),
                    "Match By": match_by,
                    "Annex": rule.get("Annex", ""),
                    "Entry No.": rule.get("Entry No.", ""),
                    "Rule Type": rule.get("Rule Type", ""),
                    "Status": status,
                    "Reason": reason,
                    "Product Category Rule": rule.get("Product Category", ""),
                    "Max Concentration (%)": rule.get("Max Concentration (%)", ""),
                    "Numeric Limit (%)": rule.get("Numeric Limit (%)", ""),
                    "Limit Basis": rule.get("Limit Basis", ""),
                    "Grouped Assessment Key": rule.get(
                        "Grouped Assessment Key", ""
                    ),
                    "Warning": rule.get("Warning", ""),
                    "Other Conditions": rule.get("Other Conditions", ""),
                    "Original Chemical Name": rule.get(
                        "Original Chemical Name", ""
                    ),
                    "INCI Mapping Method": rule.get("INCI Mapping Method", ""),
                    "Mapping Confidence": rule.get("Mapping Confidence", ""),
                    "Parent Rule ID": rule.get("Parent Rule ID", ""),
                    "Condition Code": rule.get("Condition Code", ""),
                    "Legal Source": rule.get("Legal Source", ""),
                    "Source URL": rule.get("Source URL", ""),
                }
            )

    return pd.DataFrame(rows)

def make_ingredient_summary(checks):
    severity = {
        "FAIL": 3,
        "REVIEW": 2,
        "PASS": 1,
        "NO MATCH IN DB": 0,
    }
    summary_rows = []

    for keys, grp in checks.groupby(
        ["INCI", "CAS", "EC No.", "Concentration (%)"], dropna=False
    ):
        statuses = grp["Status"].tolist()
        final_status = max(statuses, key=lambda x: severity.get(x, -1))
        annexes = " | ".join(
            sorted(
                set(
                    str(x)
                    for x in grp["Annex"]
                    if str(x).strip() not in ("", "nan")
                )
            )
        )
        reasons = " | ".join(
            dict.fromkeys(str(x) for x in grp["Reason"] if str(x).strip())
        )
        summary_rows.append(
            {
                "INCI": keys[0],
                "CAS": keys[1],
                "EC No.": keys[2],
                "Concentration (%)": keys[3],
                "Final Status": final_status,
                "Matched Annex": annexes,
                "Summary Reason": reasons,
            }
        )

    summary = pd.DataFrame(summary_rows)
    priority = {"FAIL": 0, "REVIEW": 1, "PASS": 2, "NO MATCH IN DB": 3}
    summary["_priority"] = summary["Final Status"].map(priority).fillna(9)
    return summary.sort_values("_priority").drop(columns=["_priority"])


# ---------- Exposure / Toxicology ----------
def calculate_exposure(
    formula, exposure_db, toxicology_db, exposure_category
):
    exp_match = exposure_db[
        (exposure_db["Active"].eq("Y"))
        & (exposure_db["Exposure Category"].eq(exposure_category))
    ]

    if exp_match.empty:
        raise ValueError(
            f"Exposure 시트에 '{exposure_category}' 값이 없습니다."
        )

    exp = exp_match.iloc[0]
    daily_amount_g = float(exp["Daily Amount (g/day)"])
    retention_factor = float(exp["Retention Factor"])
    body_weight_kg = float(exp["Body Weight (kg)"])

    tox = toxicology_db[toxicology_db["Active"].eq("Y")].copy()
    if "INCI_NORM" not in tox:
        tox["INCI_NORM"] = tox["INCI"].map(norm_text)
    if "CAS_NORM" not in tox:
        tox["CAS_NORM"] = tox["CAS"].map(norm_cas)

    calc_rows = []
    for _, ing in formula.iterrows():
        inci_n = norm_text(ing["INCI"])
        cas_n = norm_cas(ing["CAS"])
        tox_match = pd.DataFrame()

        if inci_n:
            tox_match = tox[tox["INCI_NORM"].eq(inci_n)]
        if tox_match.empty and cas_n:
            tox_match = tox[tox["CAS_NORM"].eq(cas_n)]

        tox_row = tox_match.iloc[0] if not tox_match.empty else None
        noael = None
        dap = None

        if tox_row is not None:
            noael = pd.to_numeric(
                pd.Series([tox_row.get("NOAEL / POD (mg/kg bw/day)")]),
                errors="coerce",
            ).iloc[0]
            dap = pd.to_numeric(
                pd.Series([tox_row.get("Dermal Absorption (%)")]),
                errors="coerce",
            ).iloc[0]

        sed = None
        mos = None
        note = ""

        if dap is None or pd.isna(dap):
            note = "DATA REQUIRED: Dermal absorption"
        else:
            sed = (
                daily_amount_g
                * 1000
                * (float(ing["Concentration (%)"]) / 100)
                * retention_factor
                * (float(dap) / 100)
            ) / body_weight_kg

            if noael is None or pd.isna(noael):
                note = "DATA REQUIRED: NOAEL/POD"
            elif sed != 0:
                mos = float(noael) / sed

        calc_rows.append(
            {
                "INCI": ing["INCI"],
                "CAS": ing["CAS"],
                "Concentration (%)": ing["Concentration (%)"],
                "Daily Amount (g/day)": daily_amount_g,
                "Retention Factor": retention_factor,
                "Body Weight (kg)": body_weight_kg,
                "NOAEL / POD (mg/kg bw/day)": (
                    None if noael is None or pd.isna(noael) else float(noael)
                ),
                "Dermal Absorption (%)": (
                    None if dap is None or pd.isna(dap) else float(dap)
                ),
                "SED (mg/kg bw/day)": sed,
                "MoS": mos,
                "Note": note,
            }
        )

    return pd.DataFrame(calc_rows)


# ---------- Files ----------
def screening_excel_bytes(
    formula, ingredient_summary, checks, grouped, exposure_results
):
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        formula.to_excel(writer, sheet_name="Formula", index=False)
        ingredient_summary.to_excel(
            writer, sheet_name="Regulatory_Summary", index=False
        )
        checks.to_excel(writer, sheet_name="Regulatory_Detail", index=False)
        grouped.to_excel(writer, sheet_name="Grouped_Assessment", index=False)
        exposure_results.to_excel(
            writer, sheet_name="Exposure_MoS", index=False
        )
    bio.seek(0)
    return bio.getvalue()


# ---------- AI ----------
def generate_cpsr_draft(
    api_key,
    model,
    product_name,
    user_product_type,
    exposure_category,
    reg_product_category,
    rinse_type,
    formula,
    ingredient_summary,
    checks,
    grouped,
    exposure_results,
):
    client = OpenAI(api_key=api_key)

    payload = {
        "product_name": product_name,
        "product_type": user_product_type,
        "exposure_category": exposure_category,
        "regulatory_product_category": reg_product_category,
        "leave_on_rinse_off": rinse_type,
        "formula": formula.where(pd.notnull(formula), None).to_dict(
            orient="records"
        ),
        "ingredient_regulatory_summary": ingredient_summary.where(
            pd.notnull(ingredient_summary), None
        ).to_dict(orient="records"),
        "detailed_regulatory_matches": checks.where(
            pd.notnull(checks), None
        ).to_dict(orient="records"),
        "grouped_assessment_items": grouped.where(
            pd.notnull(grouped), None
        ).to_dict(orient="records"),
        "exposure_and_mos": exposure_results.where(
            pd.notnull(exposure_results), None
        ).to_dict(orient="records"),
    }

    prompt = f"""
You are assisting a qualified cosmetic safety assessor preparing an EU CPSR working draft.

STRICT RULES:
- Use only INPUT_DATA.
- Never invent legal limits, NOAEL, POD, dermal absorption, warnings, test results, sources, pages or safety conclusions.
- FAIL means a prohibited substance or exceeded simple numeric restriction was identified by the Python rule engine.
- REVIEW means regulatory conditions require manual interpretation or a grouped/special-basis assessment.
- NO MATCH IN DB does NOT mean regulatory compliance.
- Treat alias matches as valid database identifiers but preserve the underlying Annex entry and condition.
- If Grouped Assessment Key or special Limit Basis is present, clearly state that direct concentration comparison is not sufficient.
- If evidence is missing, state DATA REQUIRED.
- Do not represent this document as a final or signed CPSR.

Prepare professional English:
1. Product overview
2. Composition commentary
3. Regulatory screening summary
4. FAIL items
5. REVIEW items
6. Grouped / special-basis regulatory assessments
7. Exposure / SED / MoS commentary
8. Toxicological data gaps
9. Safety assessor review points
10. Missing information checklist
11. Draft-status disclaimer

INPUT_DATA:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""

    response = client.responses.create(model=model, input=prompt)
    return response.output_text


# ============================ UI ============================
st.title("🧪 CPSR AI Screening")
st.caption(
    "EU Annex 규제 Screening · SED/MoS 계산 · CPSR Working Draft"
)

with st.sidebar:
    st.header("설정")

    db_mode = st.radio(
        "Master DB",
        ["앱 기본 DB 사용", "다른 DB 임시 업로드"],
        help="기본 DB는 앱에 포함된 Master DB입니다.",
    )

    uploaded_db = None
    if db_mode == "다른 DB 임시 업로드":
        uploaded_db = st.file_uploader(
            "Master DB (.xlsx)",
            type=["xlsx"],
            key="db_upload",
        )

    st.divider()
    st.subheader("AI Draft 설정")
    use_secret_key = bool(
        st.secrets.get("OPENAI_API_KEY", "")
        if hasattr(st, "secrets")
        else False
    )

    if use_secret_key:
        st.success("서버에 OpenAI API Key가 설정되어 있습니다.")
        api_key = st.secrets["OPENAI_API_KEY"]
    else:
        api_key = st.text_input(
            "OpenAI API Key",
            type="password",
            help="규제 Screening만 할 때는 입력하지 않아도 됩니다.",
        )

    model = st.text_input(
        "OpenAI model",
        value="gpt-5",
        help="계정에서 사용할 수 있는 모델명으로 변경할 수 있습니다.",
    )

    st.divider()
    st.caption(
        "이 앱의 자동 판정은 규제 검토 지원용이며 최종 CPSR 및 Part B 결론을 대체하지 않습니다."
    )

# DB load
try:
    if db_mode == "다른 DB 임시 업로드":
        if uploaded_db is None:
            st.info("왼쪽에서 Master DB를 업로드하면 시작할 수 있습니다.")
            st.stop()
        regulatory_db, exposure_db, toxicology_db, mapping_db = (
            load_db_from_bytes(uploaded_db.getvalue())
        )
        db_label = uploaded_db.name
    else:
        if not DEFAULT_DB.exists():
            st.error(
                "앱 폴더에서 기본 Master DB를 찾을 수 없습니다. "
                "`CPSR_Master_DB_All_INCI_Aliases_for_Colab.xlsx`를 app.py와 같은 폴더에 넣어주세요."
            )
            st.stop()
        regulatory_db, exposure_db, toxicology_db, mapping_db = (
            load_db_from_path(str(DEFAULT_DB))
        )
        db_label = DEFAULT_DB.name
except Exception as e:
    st.error(f"Master DB 로딩 오류: {e}")
    st.stop()

with st.expander("현재 Master DB 정보"):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Regulatory", f"{len(regulatory_db):,}")
    c2.metric("Exposure", f"{len(exposure_db):,}")
    c3.metric("Toxicology", f"{len(toxicology_db):,}")
    c4.metric("Product Mapping", f"{len(mapping_db):,}")
    st.caption(f"사용 중인 DB: {db_label}")

st.subheader("1. 제품 및 처방")

col1, col2 = st.columns([1.2, 1])
with col1:
    product_name = st.text_input(
        "제품명",
        value="",
        placeholder="예: MEDIPEEL TEST CREAM",
    )

available_types = mapping_db.loc[
    mapping_db["Active"].eq("Y"), "User Product Type"
].dropna().astype(str).tolist()

if not available_types:
    st.error("Product_Mapping 시트에 Active=Y 제품 유형이 없습니다.")
    st.stop()

with col2:
    user_product_type = st.selectbox(
        "제품 유형",
        available_types,
    )

formula_file = st.file_uploader(
    "처방 Excel 업로드",
    type=["xlsx"],
    help="필수 컬럼: INCI, Concentration (%). CAS / EC No. 입력을 권장합니다.",
)

if formula_file is None:
    st.info("처방 Excel을 업로드하면 규제 Screening을 시작할 수 있습니다.")
    st.stop()

try:
    formula = read_formula(formula_file)
except Exception as e:
    st.error(f"처방 파일 오류: {e}")
    st.stop()

map_match = mapping_db[
    (mapping_db["Active"].eq("Y"))
    & (mapping_db["User Product Type"].astype(str).eq(user_product_type))
]
if map_match.empty:
    st.error("선택한 제품 유형을 Product_Mapping에서 찾지 못했습니다.")
    st.stop()

m = map_match.iloc[0]
exposure_category = m["Exposure Category"]
reg_product_category = m["Regulatory Product Category"]
rinse_type = m["Leave-on / Rinse-off"]

total = formula["Concentration (%)"].sum()
m1, m2, m3, m4 = st.columns(4)
m1.metric("처방 성분 수", f"{len(formula):,}")
m2.metric("처방 총합", f"{total:.4f}%")
m3.metric("Exposure", str(exposure_category))
m4.metric("Leave/Rinse", str(rinse_type))

if abs(total - 100) > 0.05:
    st.warning("처방 총합이 100%에서 ±0.05%를 벗어납니다. 처방을 확인하세요.")

with st.expander("업로드한 처방 보기", expanded=False):
    st.dataframe(
        formula[
            ["INCI", "CAS", "EC No.", "Concentration (%)", "Function"]
        ],
        use_container_width=True,
        hide_index=True,
    )

if st.button("🔎 규제 Screening 시작", type="primary", use_container_width=True):
    with st.spinner("규제 DB와 처방을 비교하고 있습니다..."):
        try:
            checks = run_regulatory_screening(
                formula,
                regulatory_db,
                reg_product_category,
                rinse_type,
            )
            priority = {
                "FAIL": 0,
                "REVIEW": 1,
                "PASS": 2,
                "NO MATCH IN DB": 3,
            }
            checks["_priority"] = checks["Status"].map(priority).fillna(9)
            checks = checks.sort_values(
                ["_priority", "INCI", "Annex", "Entry No."],
                kind="stable",
            ).drop(columns=["_priority"])

            ingredient_summary = make_ingredient_summary(checks)
            grouped = checks[
                checks["Grouped Assessment Key"]
                .fillna("")
                .astype(str)
                .str.strip()
                .ne("")
            ].copy()

            exposure_results = calculate_exposure(
                formula,
                exposure_db,
                toxicology_db,
                exposure_category,
            )

            st.session_state["analysis"] = {
                "checks": checks,
                "ingredient_summary": ingredient_summary,
                "grouped": grouped,
                "exposure_results": exposure_results,
                "formula": formula,
                "product_name": product_name,
                "user_product_type": user_product_type,
                "exposure_category": exposure_category,
                "reg_product_category": reg_product_category,
                "rinse_type": rinse_type,
            }
            st.session_state.pop("draft", None)
        except Exception as e:
            st.error(f"Screening 중 오류: {e}")

analysis = st.session_state.get("analysis")
if not analysis:
    st.stop()

checks = analysis["checks"]
ingredient_summary = analysis["ingredient_summary"]
grouped = analysis["grouped"]
exposure_results = analysis["exposure_results"]

st.divider()
st.subheader("2. 규제 Screening 결과")

counts = ingredient_summary["Final Status"].value_counts().to_dict()
c1, c2, c3, c4 = st.columns(4)
c1.metric("FAIL", counts.get("FAIL", 0))
c2.metric("REVIEW", counts.get("REVIEW", 0))
c3.metric("PASS", counts.get("PASS", 0))
c4.metric("DB 미매칭", counts.get("NO MATCH IN DB", 0))

filter_status = st.multiselect(
    "표시할 판정",
    ["FAIL", "REVIEW", "PASS", "NO MATCH IN DB"],
    default=["FAIL", "REVIEW"],
)

summary_view = ingredient_summary[
    ingredient_summary["Final Status"].isin(filter_status)
].copy()

st.dataframe(
    summary_view,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Concentration (%)": st.column_config.NumberColumn(format="%.8f"),
    },
)

tabs = st.tabs(
    ["상세 규제 매칭", "Grouped Assessment", "SED / MoS", "다운로드"]
)

with tabs[0]:
    st.dataframe(
        checks,
        use_container_width=True,
        hide_index=True,
    )

with tabs[1]:
    if grouped.empty:
        st.success("Grouped Assessment 대상이 없습니다.")
    else:
        cols = [
            "INCI",
            "Concentration (%)",
            "Annex",
            "Entry No.",
            "Grouped Assessment Key",
            "Limit Basis",
            "Product Category Rule",
            "Numeric Limit (%)",
            "Status",
            "Reason",
        ]
        st.dataframe(
            grouped[[c for c in cols if c in grouped.columns]],
            use_container_width=True,
            hide_index=True,
        )

with tabs[2]:
    st.dataframe(
        exposure_results,
        use_container_width=True,
        hide_index=True,
    )
    missing_tox = exposure_results[
        exposure_results["Note"].fillna("").ne("")
    ]
    if not missing_tox.empty:
        st.warning(
            f"독성/흡수 데이터가 부족한 성분이 {len(missing_tox)}개 있습니다."
        )

with tabs[3]:
    result_xlsx = screening_excel_bytes(
        analysis["formula"],
        ingredient_summary,
        checks,
        grouped,
        exposure_results,
    )
    st.download_button(
        "⬇️ Screening 결과 Excel",
        data=result_xlsx,
        file_name="CPSR_AI_Screening_Result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

st.divider()
st.subheader("3. CPSR Working Draft")

st.caption(
    "이 단계만 OpenAI API를 사용합니다. Screening 결과만 필요하면 실행하지 않아도 됩니다."
)

if st.button("✨ CPSR Draft 생성", use_container_width=True):
    if not api_key:
        st.error("왼쪽 사이드바에 OpenAI API Key를 입력하세요.")
    else:
        with st.spinner("CPSR Working Draft를 생성하고 있습니다..."):
            try:
                draft = generate_cpsr_draft(
                    api_key=api_key,
                    model=model,
                    product_name=analysis["product_name"],
                    user_product_type=analysis["user_product_type"],
                    exposure_category=analysis["exposure_category"],
                    reg_product_category=analysis["reg_product_category"],
                    rinse_type=analysis["rinse_type"],
                    formula=analysis["formula"],
                    ingredient_summary=ingredient_summary,
                    checks=checks,
                    grouped=grouped,
                    exposure_results=exposure_results,
                )
                st.session_state["draft"] = draft
            except Exception as e:
                st.error(f"AI Draft 생성 오류: {e}")

draft = st.session_state.get("draft")
if draft:
    st.text_area(
        "CPSR Working Draft",
        draft,
        height=650,
    )
    st.download_button(
        "⬇️ CPSR Working Draft (.txt)",
        data=draft.encode("utf-8"),
        file_name="CPSR_Working_Draft.txt",
        mime="text/plain",
        use_container_width=True,
    )

st.divider()
st.caption(
    "자동 Screening은 내부 검토 지원용입니다. 최신 규정, 원료 특성, 노출 시나리오 및 Safety Assessor의 전문적 판단을 최종적으로 확인해야 합니다."
)
