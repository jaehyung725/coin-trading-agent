"""Provide direct access to Streamlit's built-in theme choices."""

import streamlit as st


render_theme_picker = st.components.v2.component(
    "paper_theme_picker",
    html="""
    <div class="theme-picker">
      <button type="button">다크 모드로 전환</button>
    </div>
    """,
    css="""
    .theme-picker {
      display: flex;
      justify-content: flex-end;
    }
    button {
      padding: 0.35rem 0.65rem;
      border: 1px solid var(--st-border-color);
      border-radius: var(--st-base-radius);
      background: var(--st-secondary-background-color);
      color: var(--st-text-color);
      font: inherit;
      font-size: 0.875rem;
      white-space: nowrap;
      cursor: pointer;
    }
    button:hover, button:focus-visible {
      border-color: var(--st-primary-color);
      color: var(--st-primary-color);
    }
    """,
    js="""
    export default function ({ parentElement }) {
        const button = parentElement.querySelector("button");
        const app = document.querySelector('[data-testid="stApp"]');
        const isDark = () => app && getComputedStyle(app).colorScheme === "dark";
        function updateLabel() {
            button.textContent = isDark() ? "라이트 모드로 전환" : "다크 모드로 전환";
        }
        const observer = new MutationObserver(updateLabel);
        if (app) observer.observe(app, { attributes: true, attributeFilter: ["class", "style"] });
        updateLabel();

        // Streamlit has no Python setter for a viewer's theme. Use its menu action
        // so the native widgets theme together and the browser keeps the preference.
        button.onclick = () => {
            const menu = document.querySelector('[data-testid="stMainMenuButton"]');
            if (!menu) return;
            const target = isDark() ? "Light" : "Dark";
            function chooseTheme() {
                const option = document.querySelector(`[data-testid="stMainMenuItem-theme-${target}"]`);
                if (option) option.click();
                if (menu.getAttribute("aria-expanded") === "true") menu.click();
                requestAnimationFrame(() => button.focus());
            }
            if (menu.getAttribute("aria-expanded") === "true") {
                chooseTheme();
            } else {
                menu.click();
                requestAnimationFrame(chooseTheme);
            }
        };
        return () => observer.disconnect();
    }
    """,
)
