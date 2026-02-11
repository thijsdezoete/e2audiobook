function showToast(message, type) {
    type = type || "info";
    var container = document.getElementById("toast-container");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(function() {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.3s";
        setTimeout(function() { toast.remove(); }, 300);
    }, 4000);
}

(function() {
    var evtSource = null;

    function connectSSE() {
        if (evtSource) {
            evtSource.close();
        }
        evtSource = new EventSource("/api/queue/events");

        evtSource.onmessage = function(event) {
            try {
                var msg = JSON.parse(event.data);
                var data = typeof msg.data === "string" ? JSON.parse(msg.data) : msg.data;
                var type = msg.event || event.type;

                if (type === "job_started") {
                    showToast("Converting: " + data.title, "info");
                } else if (type === "job_completed") {
                    showToast("Complete: " + data.title, "success");
                } else if (type === "job_failed") {
                    showToast("Failed: " + (data.title || "Job #" + data.job_id), "error");
                } else if (type === "chapter_completed") {
                    var bar = document.querySelector(".progress-fill");
                    if (bar && data.total) {
                        bar.style.width = (data.chapter / data.total * 100) + "%";
                    }
                    var txt = document.querySelector(".progress-text");
                    if (txt && data.total) {
                        txt.textContent = "Chapter " + data.chapter + " / " + data.total;
                    }
                } else if (type === "queue_paused") {
                    showToast("Queue paused", "info");
                } else if (type === "queue_resumed") {
                    showToast("Queue resumed", "info");
                }
            } catch (e) {
                // ignore parse errors
            }
        };

        evtSource.onerror = function() {
            evtSource.close();
            setTimeout(connectSSE, 5000);
        };
    }

    connectSSE();
})();

document.addEventListener("htmx:afterRequest", function(event) {
    if (event.detail.successful && event.detail.verb !== "get") {
        showToast("Done", "success");
    } else if (!event.detail.successful && event.detail.verb !== "get") {
        var msg = "Request failed";
        try {
            var resp = JSON.parse(event.detail.xhr.responseText);
            if (resp.detail) msg = resp.detail;
        } catch(e) {}
        showToast(msg, "error");
    }
});
