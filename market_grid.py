"""Compact, selectable market table for the Streamlit sidebar."""

import streamlit as st


render_market_grid = st.components.v2.component(
    "krw_market_grid",
    html="""
    <div class="viewport" aria-label="업비트 원화 종목">
      <div class="header">
        <span>종목</span><span>현재가</span><span>전일 대비</span><span>거래대금</span>
      </div>
      <div class="rows"></div>
    </div>
    """,
    css="""
    .viewport {
      overflow: auto;
      max-height: 31rem;
      border: 1px solid #cbd9e2;
      border-radius: 0.65rem;
      background: #fff;
      color: #19323e;
    }
    .header, .row {
      display: grid;
      grid-template-columns: minmax(52px, 1.25fr) minmax(0, 1.15fr) minmax(0, 0.9fr) minmax(0, 0.95fr);
      align-items: center;
      column-gap: 5px;
      padding: 7px 6px;
      min-width: 226px;
      box-sizing: border-box;
      text-align: right;
    }
    .header {
      position: sticky;
      z-index: 1;
      top: 0;
      background: #e9f2f6;
      color: #345062;
      font-size: 10px;
      font-weight: 700;
      white-space: nowrap;
    }
    .row {
      width: 100%;
      min-height: 43px;
      border: 0;
      border-bottom: 1px solid #e8eef2;
      border-left: 3px solid transparent;
      padding-left: 3px;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font: inherit;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
    }
    .header > :first-child, .identity { text-align: left; }
    .identity { min-width: 0; }
    .identity > * {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    small { color: #526979; font-size: 10px; }
    .row > :not(.identity) { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .row:hover { background: #f0f6fb; }
    .row[aria-pressed="true"] {
      background: #e4f0ff;
      border-left-color: #1769c2;
    }
    .row:focus-visible { outline: 2px solid #1769c2; outline-offset: -2px; }
    .up { color: #125bb0; font-weight: 700; }
    .down { color: #b42341; font-weight: 700; }
    .turnover { color: #405768; }
    """,
    js="""
    export default function ({ parentElement, data, setTriggerValue }) {
        const list = parentElement.querySelector(".rows");
        const focus = parentElement.activeElement?.dataset.market;
        const fragment = document.createDocumentFragment();
        for (const item of data.rows) {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "row";
            row.dataset.market = item.code;
            row.setAttribute("aria-pressed", String(item.code === data.selected));
            row.setAttribute("aria-label", [
                item.name, item.symbol, `현재가 ${item.price}원`,
                `전일 대비 ${item.change}`, `거래대금 ${item.turnover}원`
            ].join(", "));

            const identity = document.createElement("span");
            identity.className = "identity";
            const name = document.createElement("strong");
            name.textContent = item.name;
            name.title = item.name;
            const symbol = document.createElement("small");
            symbol.textContent = item.symbol;
            identity.append(name, symbol);
            row.append(identity);
            for (const [value, style] of [
                [item.price, "price"], [item.change, item.direction], [item.turnover, "turnover"]
            ]) {
                const cell = document.createElement("span");
                cell.textContent = value;
                cell.title = value;
                cell.className = style;
                row.append(cell);
            }

            row.onclick = () => setTriggerValue("selected", item.code);
            fragment.append(row);
        }
        list.replaceChildren(fragment);
        if (focus) {
            Array.from(list.children).find(row => row.dataset.market === focus)?.focus({preventScroll: true});
        }
    }
    """,
)
