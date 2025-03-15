const express = require("express");
const cors = require("cors");

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

app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
});













  
















  





