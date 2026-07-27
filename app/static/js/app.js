function chart(id, labels, values, type='bar'){
    const el = document.getElementById(id);
    if(!el) return;
    const isBar = type === 'bar';
    const isSingleBar = isBar && values.length === 1;

    new Chart(el, {
        type,
        data: {
            labels,
            datasets: [{
                label: 'Ventas',
                data: values,
                borderWidth: 2,
                barPercentage: isSingleBar ? 0.55 : 0.7,
                categoryPercentage: isSingleBar ? 0.5 : 0.72,
                maxBarThickness: isSingleBar ? 72 : 48
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {duration: 220},
            plugins: {
                legend: {display: !isBar},
                tooltip: {
                    backgroundColor: "#202435",
                    padding: 12,
                    cornerRadius: 10,
                    titleFont: {weight: "700"}
                }
            },
            scales: isBar ? {
                x: {grid: {display: false}},
                y: {beginAtZero: true, ticks: {precision: 0}}
            } : undefined
        }
    });
}
chart('topProducts', window.topLabels||[], window.topValues||[]);
chart('categoryChart', window.catLabels||[], window.catValues||[], 'doughnut');

function reportMoney(value) {
    return Number(value || 0).toLocaleString(
        document.documentElement.lang === "en" ? "en-US" : "es-MX",
        {style: "currency", currency: "MXN"}
    );
}

function initializeReportCharts() {
    const report = window.reportAnalytics;
    if (!report || typeof Chart === "undefined") return;

    const trendCanvas = document.getElementById("reportTrendChart");
    if (trendCanvas) {
        const locale = document.documentElement.lang === "en" ? "en-US" : "es-MX";
        const labels = report.daily.map(point => new Date(`${point.date}T12:00:00`).toLocaleDateString(
            locale,
            {day: "numeric", month: "short"}
        ));
        const trendDatasets = [
            {
                label: report.labels.sales,
                data: report.daily.map(point => point.sales),
                borderColor: "#6956e8",
                backgroundColor: "rgba(105, 86, 232, .10)",
                pointBackgroundColor: "#6956e8",
                borderWidth: 2.5,
                pointRadius: report.daily.length > 60 ? 0 : 3,
                tension: .28,
                fill: true
            }
        ];
        if (report.advanced) {
            trendDatasets.push({
                label: report.labels.profit,
                data: report.daily.map(point => point.profit),
                borderColor: "#20a88a",
                backgroundColor: "rgba(32, 168, 138, .06)",
                pointBackgroundColor: "#20a88a",
                borderWidth: 2.5,
                pointRadius: report.daily.length > 60 ? 0 : 3,
                tension: .28
            });
        }
        new Chart(trendCanvas, {
            type: "line",
            data: {
                labels,
                datasets: trendDatasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {duration: 220},
                interaction: {mode: "index", intersect: false},
                plugins: {
                    legend: {
                        display: true,
                        position: "bottom",
                        labels: {usePointStyle: true, padding: 18, color: "#555d70"}
                    },
                    tooltip: {
                        backgroundColor: "#202435",
                        padding: 12,
                        cornerRadius: 10,
                        titleFont: {weight: "700"},
                        callbacks: {
                            label: context => `${context.dataset.label}: ${reportMoney(context.raw)}`
                        }
                    }
                },
                scales: {
                    x: {grid: {display: false}, ticks: {maxTicksLimit: 10}},
                    y: {
                        beginAtZero: true,
                        ticks: {callback: value => reportMoney(value)}
                    }
                }
            }
        });
    }

    const paymentCanvas = document.getElementById("reportPaymentsChart");
    if (paymentCanvas) {
        new Chart(paymentCanvas, {
            type: "doughnut",
            data: {
                labels: report.payments.map(payment => payment.label),
                datasets: [{
                    data: report.payments.map(payment => payment.amount),
                    backgroundColor: ["#6956e8", "#2e8bd3", "#20a88a", "#e6aa45"],
                    borderColor: "#ffffff",
                    borderWidth: 3,
                    hoverOffset: 5
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {duration: 220},
                cutout: "68%",
                plugins: {
                    legend: {display: false},
                    tooltip: {
                        backgroundColor: "#202435",
                        padding: 12,
                        cornerRadius: 10,
                        titleFont: {weight: "700"},
                        callbacks: {
                            label: context => {
                                const payment = report.payments[context.dataIndex];
                                return `${payment.label}: ${reportMoney(payment.amount)} · ${payment.percentage}% · ${payment.tickets} ${report.labels.tickets.toLowerCase()}`;
                            }
                        }
                    }
                }
            }
        });
    }
}

function initializeExecutiveChart() {
    const analytics = window.executiveAnalytics;
    const canvas = document.getElementById("executiveTrendChart");
    if (!analytics || !canvas || typeof Chart === "undefined") return;

    const locale = document.documentElement.lang === "en" ? "en-US" : "es-MX";
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const labels = analytics.current.map(point => new Date(`${point.date}T12:00:00`).toLocaleDateString(
        locale,
        {day: "numeric", month: "short"}
    ));

    new Chart(canvas, {
        type: "line",
        data: {
            labels,
            datasets: [
                {
                    label: analytics.labels.sales,
                    data: analytics.current.map(point => Number(point.sales)),
                    borderColor: "#6752e8",
                    backgroundColor: "rgba(103, 82, 232, .10)",
                    pointBackgroundColor: "#6752e8",
                    borderWidth: 2.5,
                    pointRadius: analytics.current.length > 45 ? 0 : 2.5,
                    tension: .3,
                    fill: true
                },
                {
                    label: analytics.labels.profit,
                    data: analytics.current.map(point => Number(point.profit)),
                    borderColor: "#159879",
                    backgroundColor: "transparent",
                    pointBackgroundColor: "#159879",
                    borderWidth: 2.25,
                    pointRadius: analytics.current.length > 45 ? 0 : 2.5,
                    tension: .3
                },
                {
                    label: analytics.labels.previous,
                    data: analytics.previous.map(point => Number(point.sales)),
                    borderColor: "#aeb5c5",
                    backgroundColor: "transparent",
                    borderDash: [6, 5],
                    pointRadius: 0,
                    borderWidth: 1.8,
                    tension: .3
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: {duration: reducedMotion ? 0 : 180},
            interaction: {mode: "index", intersect: false},
            plugins: {
                legend: {
                    position: "bottom",
                    labels: {
                        usePointStyle: true,
                        padding: 18,
                        color: "#555d70",
                        boxWidth: 8,
                        boxHeight: 8
                    }
                },
                tooltip: {
                    backgroundColor: "#202435",
                    padding: 12,
                    cornerRadius: 10,
                    titleFont: {weight: "700"},
                    callbacks: {
                        label: context => `${context.dataset.label}: ${reportMoney(context.raw)}`
                    }
                }
            },
            scales: {
                x: {
                    grid: {display: false},
                    ticks: {maxTicksLimit: 8, color: "#687085"}
                },
                y: {
                    beginAtZero: true,
                    grid: {color: "rgba(218, 222, 232, .65)"},
                    ticks: {
                        color: "#687085",
                        callback: value => reportMoney(value)
                    }
                }
            }
        }
    });
}

function initializeCustomReportPeriod() {
    const toggle = document.querySelector("[data-custom-period-toggle]");
    const form = document.getElementById("custom-period");
    if (!toggle || !form) return;

    toggle.addEventListener("click", event => {
        event.returnValue = false;
        form.hidden = false;
        toggle.classList.add("is-active");
        toggle.setAttribute("aria-current", "page");
        toggle.setAttribute("aria-expanded", "true");
        document.getElementById("report-start")?.focus();
        form.scrollIntoView({block: "nearest"});
    });
}

initializeReportCharts();
initializeExecutiveChart();
initializeCustomReportPeriod();

function initializePatiaSelects() {
    const selects = document.querySelectorAll("select:not([multiple]):not([data-native-select])");

    function closeSelect(component, restoreFocus = false) {
        const button = component.querySelector(".patia-select__trigger");
        const listbox = component.querySelector(".patia-select__listbox");
        button.setAttribute("aria-expanded", "false");
        component.classList.remove("is-open");
        listbox.hidden = true;
        if (restoreFocus) button.focus();
    }

    function openSelect(component) {
        document.querySelectorAll(".patia-select.is-open").forEach(openComponent => {
            if (openComponent !== component) closeSelect(openComponent);
        });
        const button = component.querySelector(".patia-select__trigger");
        const listbox = component.querySelector(".patia-select__listbox");
        component.classList.add("is-open");
        button.setAttribute("aria-expanded", "true");
        listbox.hidden = false;
        const selected = listbox.querySelector('[aria-selected="true"]');
        (selected || listbox.querySelector('[role="option"]'))?.focus();
    }

    selects.forEach((select, selectIndex) => {
        const component = document.createElement("div");
        const trigger = document.createElement("button");
        const valueLabel = document.createElement("span");
        const chevron = document.createElement("i");
        const listbox = document.createElement("div");
        const listboxId = `${select.id || `patia-select-${selectIndex}`}-listbox`;
        const fieldLabel = select.id
            ? document.querySelector(`label[for="${CSS.escape(select.id)}"]`)
            : null;

        component.className = "patia-select";
        trigger.className = "patia-select__trigger";
        trigger.type = "button";
        trigger.setAttribute("role", "combobox");
        trigger.setAttribute("aria-haspopup", "listbox");
        trigger.setAttribute("aria-expanded", "false");
        trigger.setAttribute("aria-controls", listboxId);
        if (fieldLabel) trigger.setAttribute("aria-label", fieldLabel.textContent.trim());
        valueLabel.className = "patia-select__value";
        chevron.className = "fa-solid fa-chevron-down";
        chevron.setAttribute("aria-hidden", "true");
        trigger.append(valueLabel, chevron);

        listbox.id = listboxId;
        listbox.className = "patia-select__listbox";
        listbox.setAttribute("role", "listbox");
        listbox.hidden = true;

        const optionButtons = [...select.options].map((option, optionIndex) => {
            const optionButton = document.createElement("button");
            optionButton.className = "patia-select__option";
            optionButton.type = "button";
            optionButton.id = `${listboxId}-option-${optionIndex}`;
            optionButton.setAttribute("role", "option");
            optionButton.dataset.value = option.value;
            optionButton.textContent = option.textContent;
            optionButton.disabled = option.disabled;

            optionButton.addEventListener("click", () => {
                select.value = option.value;
                select.dispatchEvent(new Event("change", {bubbles: true}));
                closeSelect(component, true);
            });
            return optionButton;
        });

        listbox.append(...optionButtons);
        component.append(trigger, listbox);
        select.insertAdjacentElement("afterend", component);
        select.classList.add("patia-native-select");

        function syncFromNative() {
            const selectedIndex = Math.max(select.selectedIndex, 0);
            const selectedOption = select.options[selectedIndex];
            valueLabel.textContent = selectedOption?.textContent || "";
            optionButtons.forEach((optionButton, optionIndex) => {
                const selected = optionIndex === selectedIndex;
                optionButton.setAttribute("aria-selected", String(selected));
                optionButton.classList.toggle("is-selected", selected);
            });
            trigger.disabled = select.disabled;
            component.classList.toggle("is-disabled", select.disabled);
            if (select.validity.valid) trigger.removeAttribute("aria-invalid");
        }

        function moveOption(current, direction) {
            const enabledOptions = optionButtons.filter(option => !option.disabled);
            const currentIndex = Math.max(enabledOptions.indexOf(current), 0);
            enabledOptions[(currentIndex + direction + enabledOptions.length) % enabledOptions.length]?.focus();
        }

        trigger.addEventListener("click", () => {
            component.classList.contains("is-open") ? closeSelect(component) : openSelect(component);
        });
        trigger.addEventListener("keydown", event => {
            if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
                event.preventDefault();
                openSelect(component);
            }
        });
        listbox.addEventListener("keydown", event => {
            const current = document.activeElement;
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                moveOption(current, event.key === "ArrowDown" ? 1 : -1);
            } else if (event.key === "Home" || event.key === "End") {
                event.preventDefault();
                const enabled = optionButtons.filter(option => !option.disabled);
                enabled[event.key === "Home" ? 0 : enabled.length - 1]?.focus();
            } else if (event.key === "Escape") {
                event.preventDefault();
                closeSelect(component, true);
            } else if (event.key === "Tab") {
                closeSelect(component);
            }
        });
        select.addEventListener("change", syncFromNative);
        select.addEventListener("invalid", () => {
            trigger.setAttribute("aria-invalid", "true");
            trigger.focus();
        });
        select.form?.addEventListener("reset", () => requestAnimationFrame(syncFromNative));
        syncFromNative();
    });

    document.addEventListener("pointerdown", event => {
        document.querySelectorAll(".patia-select.is-open").forEach(component => {
            if (!component.contains(event.target)) closeSelect(component);
        });
    });
}

initializePatiaSelects();

document.addEventListener("DOMContentLoaded", () => {
    const menuToggle = document.querySelector(".sidebar-v2__toggle");
    const navigation = document.getElementById("primary-navigation");
    if (menuToggle && navigation) {
        menuToggle.addEventListener("click", () => {
            const open = navigation.classList.toggle("is-open");
            menuToggle.setAttribute("aria-expanded", String(open));
            menuToggle.querySelector("span").textContent = open
                ? menuToggle.dataset.closeLabel
                : menuToggle.dataset.openLabel;
        });
    }

    const scrollY = localStorage.getItem("patia_scroll_y");

    if (scrollY) {
        window.scrollTo(0, parseInt(scrollY));
        localStorage.removeItem("patia_scroll_y");
    }

    document.querySelectorAll(".keep-scroll").forEach(form => {
        form.addEventListener("submit", () => {
            localStorage.setItem("patia_scroll_y", window.scrollY);
        });
    });
});
document.querySelectorAll("form[data-submit-once]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
    });
  });
});
