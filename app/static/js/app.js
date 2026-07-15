function chart(id, labels, values, type='bar'){
    const el = document.getElementById(id);
    if(!el) return;

    new Chart(el, {
        type,
        data: {
            labels,
            datasets: [{
                label: 'Ventas',
                data: values,
                borderWidth: 2,
                barPercentage: 0.35,
                categoryPercentage: 0.45
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false
        }
    });
}
chart('topProducts', window.topLabels||[], window.topValues||[]);
chart('categoryChart', window.catLabels||[], window.catValues||[], 'doughnut');
document.addEventListener("DOMContentLoaded", () => {
    const menuToggle = document.querySelector(".sidebar-v2__toggle");
    const navigation = document.getElementById("primary-navigation");
    if (menuToggle && navigation) {
        menuToggle.addEventListener("click", () => {
            const open = navigation.classList.toggle("is-open");
            menuToggle.setAttribute("aria-expanded", String(open));
            menuToggle.querySelector("span").textContent = open ? "Cerrar" : "Menú";
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
