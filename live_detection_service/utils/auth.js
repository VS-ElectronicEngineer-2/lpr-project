const axios = require("axios");

const PAYMENT_TOKEN_URL = "http://220.158.208.216:3000/payment/public/lpr/token";

let paymentToken = null;
let tokenExpiry = 0; // Unix timestamp for expiration

// 🔹 Function to Refresh Token
async function refreshPaymentToken() {
    try {
        console.log("🔄 Refreshing Payment Token...");
        const response = await axios.get(PAYMENT_TOKEN_URL, {
            headers: { "Accept": "application/json" }
        });

        paymentToken = response.data.accessToken;
        tokenExpiry = Date.now() + response.data.token_expired_at * 1000; // Convert to ms
        console.log("✅ Payment Token Updated:", paymentToken);
    } catch (error) {
        console.error("❌ Failed to fetch payment token:", error.message);
        paymentToken = null;
    }
}

// 🔹 Middleware to Ensure Token is Valid Before Processing Requests
async function ensureValidToken(req, res, next) {
    if (!paymentToken || Date.now() >= tokenExpiry) {
        await refreshPaymentToken();
    }

    if (!paymentToken) {
        return res.status(500).json({ error: "Failed to authenticate payment request." });
    }

    req.paymentToken = paymentToken; // Attach token to request
    next();
}

module.exports = { ensureValidToken, refreshPaymentToken };
