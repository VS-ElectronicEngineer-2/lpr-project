const express = require("express");
const axios = require("axios");
const { ensureValidToken } = require("../utils/auth");

const router = express.Router();

const PAYMENT_GATEWAY_URL = "http://220.158.208.216:3000/payment/public/lpr/generate-qr";

// 🔹 API: Generate Payment QR
router.post("/generate-qr", ensureValidToken, async (req, res) => {
    const { totalAmount } = req.body;

    if (!totalAmount || totalAmount <= 0) {
        return res.status(400).json({ error: "Invalid payment amount" });
    }

    try {
        console.log("📤 Sending Payment Request...");
        const paymentRequestData = {
            order_number: `order_${Date.now()}`,
            override_existing_unprocessed_order_no: "YES",
            order_amount: totalAmount.toFixed(2),
            validity_qr: "99999",
            store_id: "Token",
            terminal_id: "0982722",
            shift_id: "Success",
            to_whatsapp_no: "+60123456789",
            language: "en_us",
            whatsapp_template_id: "payment_qr"
        };

        const response = await axios.post(PAYMENT_GATEWAY_URL, paymentRequestData, {
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${req.paymentToken}`
            }
        });

        if (response.data?.data?.status !== "success") {
            console.error("❌ Payment gateway error:", response.data);
            return res.status(500).json({ error: "Failed to generate payment link." });
        }

        const paymentUrl = response.data.data.content.iframe_url;
        console.log(`✅ Payment URL Generated: ${paymentUrl}`);

        res.json({ paymentUrl, qrCode: paymentUrl });
    } catch (error) {
        console.error("❌ Error Processing Payment:", error.message);
        res.status(500).json({ error: "Failed to generate QR code." });
    }
});

module.exports = router;
