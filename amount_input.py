from decimal import Decimal
from math import isfinite

import streamlit as st


_amount_field = st.components.v2.component(
    "grouped_amount",
    html='<label><span></span><input type="text" inputmode="decimal" autocomplete="off"></label>',
    css="""
    label { display: grid; gap: .375rem; color: var(--st-text-color); font-size: .875rem; }
    input {
        box-sizing: border-box;
        width: 100%;
        padding: .5rem .75rem;
        font: inherit;
        color: inherit;
        background: var(--st-secondary-background-color);
        border: 1px solid transparent;
        border-radius: var(--st-base-radius, .5rem);
    }
    input:focus { outline: 1px solid var(--st-primary-color); }
    """,
    js=r"""
    const group = (value) => {
        const [whole, fraction] = value.split(".");
        const integer = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        return fraction === undefined ? integer : integer + "." + fraction;
    };

    export default function ({ parentElement, data, setStateValue }) {
        const input = parentElement.querySelector("input");
        parentElement.querySelector("span").textContent = data.label;
        if (!input.oninput) input.value = group(data.value);

        input.oninput = () => {
            const value = input.value;
            const cursor = input.selectionStart;
            const raw = value.replaceAll(",", "");
            if (!/^\d*(\.\d*)?$/.test(raw)) {
                input.value = input.dataset.lastValue;
                input.setSelectionRange(Math.max(0, cursor - 1), Math.max(0, cursor - 1));
                return;
            }

            const digitsBeforeCursor = value.slice(0, cursor).replaceAll(",", "").length;
            input.value = group(raw);
            let nextCursor = 0;
            let remaining = digitsBeforeCursor;
            while (remaining > 0 && nextCursor < input.value.length) {
                if (input.value[nextCursor++] !== ",") remaining--;
            }
            input.setSelectionRange(nextCursor, nextCursor);
            input.dataset.lastValue = input.value;
            setStateValue("amount", raw);
        };
        input.dataset.lastValue = input.value;
    }
    """,
)


def amount_input(label, *, key, value=None, minimum=0.0):
    initial = "" if value is None else format(Decimal(str(value)), "f")
    result = _amount_field(
        key=key,
        data={"label": label, "value": initial},
        default={"amount": initial},
        on_amount_change=lambda: None,
    )
    try:
        amount = float(result.amount)
    except (TypeError, ValueError):
        return None
    if not isfinite(amount) or amount < minimum:
        st.caption(f"{minimum:,.2f} 이상의 금액을 입력하세요.")
        return None
    return amount
