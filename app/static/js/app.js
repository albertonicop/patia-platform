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
            plugins: {
                legend: {display: !isBar}
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
        new Chart(trendCanvas, {
            type: "line",
            data: {
                labels,
                datasets: [
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
                    },
                    {
                        label: report.labels.profit,
                        data: report.daily.map(point => point.profit),
                        borderColor: "#20a88a",
                        backgroundColor: "rgba(32, 168, 138, .06)",
                        pointBackgroundColor: "#20a88a",
                        borderWidth: 2.5,
                        pointRadius: report.daily.length > 60 ? 0 : 3,
                        tension: .28
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {mode: "index", intersect: false},
                plugins: {
                    legend: {display: true, position: "bottom", labels: {usePointStyle: true}},
                    tooltip: {
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
                cutout: "68%",
                plugins: {
                    legend: {display: false},
                    tooltip: {
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
        form.scrollIntoView({block: "nearest", behavior: "smooth"});
    });
}

initializeReportCharts();
initializeCustomReportPeriod();
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
