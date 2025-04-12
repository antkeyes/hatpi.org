// JS function to handle the toggle of the hamburger menu
function toggleMenu() {
    console.log("Hamburger menu clicked");
}

document.addEventListener("DOMContentLoaded", () => {
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                console.log("Revealing section:", entry.target.id); // ✅ ADDED
                entry.target.classList.add('fade-in-up');
                observer.unobserve(entry.target);
            }
        });
    }, {
        threshold: 0.1
    });

    document.querySelectorAll('[data-fade]').forEach(el => {
        console.log("Observing:", el.id); // ✅ ADDED
        observer.observe(el);
    });
});

