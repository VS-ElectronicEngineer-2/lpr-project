const express = require("express");
const cors = require("cors");
const axios = require("axios");  // ✅ Add this at the top of server.js

const summonsRoutes = require("./routes/summons");
const paymentRoutes = require("./routes/payment");
const { refreshPaymentToken } = require("./utils/auth");

const app = express();
const PORT = 5000;

app.use(cors());
app.use(express.json());

// Load Routes
app.use("/api/summons", summonsRoutes);
app.use("/api/payment", paymentRoutes);

// Refresh Token on Startup
refreshPaymentToken();

app.get("/queue-summons", (req, res) => {
    const { plate } = req.query;

    // Dummy Summons Data (Replace with real API data)
    const summonsList = [
        { noticeNo: "KN0802400002", offence: "30(c)", amount: 300 },
        { noticeNo: "KN0742400189", offence: "4", amount: 300 }
    ];

    let html = `
        <h2>Summons Details for: ${plate}</h2>
        <form id="paymentForm">
            <ul>
    `;

    summonsList.forEach(summon => {
        html += `
            <li>
                <input type="checkbox" name="summons" value="${summon.noticeNo}" data-amount="${summon.amount}">
                ${summon.noticeNo} - ${summon.offence} - RM${summon.amount}
            </li>
        `;
    });

    html += `
            </ul>
            <button type="button" onclick="generatePaymentQR()">Pay Selected</button>
        </form>

        <div id="qrCodeContainer" style="margin-top: 20px;"></div>

        <script>
            function generatePaymentQR() {
                const selectedSummons = [];
                let totalAmount = 0;

                document.querySelectorAll('input[name="summons"]:checked').forEach(checkbox => {
                    selectedSummons.push(checkbox.value);
                    totalAmount += parseFloat(checkbox.getAttribute("data-amount"));
                });

                if (selectedSummons.length === 0) {
                    alert("Please select at least one summons to pay.");
                    return;
                }

                fetch("/api/payment/generate-qr", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ summons: selectedSummons, totalAmount: totalAmount })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.qrCode) {
                        document.getElementById("qrCodeContainer").innerHTML = 
                            '<img src="' + data.qrCode + '" width="200" height="200"><br>' +
                            '<a href="' + data.paymentUrl + '" target="_blank">Pay Now</a>';
                    } else {
                        alert("Failed to generate payment QR code.");
                    }
                })
                .catch(error => console.error("Error generating payment QR:", error));
            }
        </script>
    `;

    res.send(html);
});

app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
});













  
















  





