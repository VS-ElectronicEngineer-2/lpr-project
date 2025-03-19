const express = require("express");
const axios = require("axios");
const xml2js = require("xml2js");
const path = require("path");
const fs = require("fs");


const router = express.Router();
const API_URL = "https://prm.citycarpark.my/CCP_ArchService/MessageGateway.svc";
const SOAP_ACTION = "http://www.citycarpark.my/MessageGatewayService/ProcessMessage";

let requestInProgress = {}; // Store ongoing requests

router.post("/", async (req, res) => {
    const { vehicleNumber } = req.body;

    if (!vehicleNumber) {
        return res.status(400).json({ error: "Vehicle number is required" });
    }

    // 🚨 Prevent Duplicate Requests
    if (requestInProgress[vehicleNumber]) {
        console.warn(`⚠️ Duplicate request detected for ${vehicleNumber}. Skipping...`);
        return res.status(429).json({ error: "Request already in progress. Try again later." });
    }

    console.log(`✅ Requesting summons for plate: ${vehicleNumber}`);
    requestInProgress[vehicleNumber] = true;

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
                delete requestInProgress[vehicleNumber]; // ✅ Ensure request flag is cleared
                return res.status(500).json({ error: "Failed to parse API response." });
            }
        
            try {
                const responseBody = result["s:Envelope"]["s:Body"][0]["Response"][0];
                const summonsList = responseBody["Summonses"]?.[0]["Summons"] || [];

                let formattedSummons = summonsList.map((summons) => ({
                    plate: (summons.VehicleRegistrationNo?.[0] || "Unknown").trim().toUpperCase(), // ✅ Convert to UPPERCASE & Trim
                    noticeNo: summons.NoticeNo?.[0]?.trim() || "Unknown",
                    offence: summons.OffenceSection?.[0]?.trim() || "Unknown",
                    location: summons.OffenceLocation?.[0]?.trim() || "Unknown",
                    date: summons.OffenceDate?.[0]?.trim() || "Unknown",
                    status: summons.NoticeStatus?.[0] === "T" ? "Unpaid" : "Paid",
                    amount: summons.Amount?.[0] ? parseFloat(summons.Amount[0]) : 0,
                    due_date: summons.DueDate?.[0]?.trim() || "Unknown",
                }));
        
                // ✅ Filter Only Unpaid Summons
                let unpaidSummons = formattedSummons.filter((s) => s.status === "Unpaid");

                console.log("🚀 Processed Summons:", unpaidSummons);

                delete requestInProgress[vehicleNumber]; // ✅ Clear the flag AFTER processing

                return res.json(unpaidSummons); // ✅ FIXED: Return the array directly
            } catch (error) {
                console.error("❌ Error Processing Summons:", error);
                delete requestInProgress[vehicleNumber]; // ✅ Clear flag on failure
                return res.status(500).json({ error: "Invalid API response format." });
            }
        });
    } catch (error) {
        delete requestInProgress[vehicleNumber]; // ✅ Ensure request flag is cleared on error
        console.error("❌ API Request Failed:", error.message);
        res.status(500).json({ error: "Failed to fetch summons data." });
    }
});

router.get("/download-pdf", async (req, res) => {
    const { plate } = req.query;

    if (!plate) {
        return res.status(400).json({ error: "Vehicle number is required" });
    }

    console.log(`📥 Generating PDF for plate: ${plate}`);

    // **Fetch summons data (Use Your Stored Data)**
    const summonsList = [
        {
            noticeNo: "KN0802400002",
            plate: "DCL12",
            offence: "PERINTAH 30(c)",
            location: "JALAN BUKIT SETONGKOL 7",
            date: "2024-12-31",
            status: "Unpaid",
            amount: 300,
        },
        {
            noticeNo: "KN0742400189",
            plate: "DCL12",
            offence: "PERINTAH 4",
            location: "JALAN MAT KILAU",
            date: "2024-12-24",
            status: "Unpaid",
            amount: 300,
        },
    ];

    const pdfPath = path.join(__dirname, `summons_${plate}.pdf`);

    try {
        await generateSummonsPDF(plate, summonsList, pdfPath);

        // **Send PDF File as Response**
        res.download(pdfPath, `summons_${plate}.pdf`, (err) => {
            if (err) {
                console.error("❌ Error sending PDF:", err);
                res.status(500).json({ error: "Failed to generate PDF" });
            }

            // **Delete the file after download to free storage**
            setTimeout(() => {
                fs.unlinkSync(pdfPath);
                console.log(`🗑️ Deleted: ${pdfPath}`);
            }, 5000);
        });
    } catch (error) {
        console.error("❌ PDF Generation Error:", error);
        res.status(500).json({ error: "Failed to generate PDF" });
    }
});


module.exports = router;
