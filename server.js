const express = require("express");
const axios = require("axios");
const cors = require("cors");
const bodyParser = require("body-parser");
const xml2js = require("xml2js");

const app = express();
const PORT = 5000;

app.use(cors());
app.use(bodyParser.json());

const summonsQueue = [];

const API_URL = "https://prm.citycarpark.my/CCP_ArchService/MessageGateway.svc";
const SOAP_ACTION = "http://www.citycarpark.my/MessageGatewayService/ProcessMessage";

app.post("/api/summons", async (req, res) => {
    const { vehicleNumber } = req.body;

    if (!vehicleNumber) {
        return res.status(400).json({ error: "Vehicle number is required" });
    }

    console.log(`✅ Requesting summons for plate: ${vehicleNumber}`);

    const requestXML = `<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
    <s:Header>
        <RequestCode>REQ_11</RequestCode>
        <AgencyID>VISTAAPP</AgencyID>
        <AgencyKey>HISV2024@APP</AgencyKey>
    </s:Header>
    <s:Body>
        <Request>
            <OffenderIDNo></OffenderIDNo>
            <VehicleRegistrationNumber>${vehicleNumber}</VehicleRegistrationNumber>
            <NoticeNo></NoticeNo>
        </Request>
    </s:Body>
</s:Envelope>`;

    try {
        console.log("🚀 Sending SOAP Request...");

        const response = await axios.post(API_URL, requestXML, {
            headers: {
                "Content-Type": "text/xml; charset=utf-8",
                SOAPAction: SOAP_ACTION,
            },
        });

        console.log("📥 RAW API RESPONSE:", response.data);

        xml2js.parseString(response.data, (err, result) => {
            if (err) {
                console.error("❌ XML Parsing Error:", err);
                return res.status(500).json({ error: "Failed to parse API response." });
            }

            try {
                const responseBody = result["s:Envelope"]["s:Body"][0];

                if (!responseBody || !responseBody["Response"]) {
                    console.error("❌ Unexpected Response Format:", responseBody);
                    return res.status(500).json({ error: "Invalid API response format." });
                }

                const summonsList =
                    responseBody["Response"][0]["Summonses"]?.[0]["Summons"] || [];

                if (summonsList.length === 0) {
                    return res.json({ message: "No summons found." });
                }

                let formattedSummons = summonsList.map((summons) => ({
                    plate: summons.VehicleRegistrationNo?.[0] || "Unknown",
                    noticeNo: summons.NoticeNo?.[0] || "Unknown",
                    offence: summons.OffenceDescription?.[0] || "Unknown",
                    location: summons.OffenceLocation?.[0] || "Unknown",
                    date: summons.OffenceDate?.[0] || "Unknown",
                    status: summons.NoticeStatus?.[0] === "T" ? "Unpaid" : "Paid",
                    amount: summons.Amount?.[0] ? `RM${summons.Amount[0]}` : "Unknown",
                    due_date: summons.DueDate?.[0] || "Unknown",
                }));

                formattedSummons.forEach((summons) => {
                    if (!summonsQueue.some((s) => s.noticeNo === summons.noticeNo)) {
                        summonsQueue.push(summons);
                    }
                });

                console.log("✅ Summons Updated:", formattedSummons);
                res.json({ summonsQueue });
            } catch (error) {
                console.error("❌ Error in Processing:", error);
                return res.status(500).json({ error: "Invalid API response format." });
            }
        });
    } catch (error) {
        console.error("❌ API Request Failed:", error.message);
        res.status(500).json({ error: "Failed to fetch summons data." });
    }
});

app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
});








  





