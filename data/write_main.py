import base64, os
data = "IyBTTCBDb21wYXJpc29uOiBDdXJyZW50IFN5c3RlbSB2cyBTNSAoU3VwZXJUcmVuZCBhcyBTTCkKaW1wb3J0IHNxbGl0ZTMsIG9zLCBzeXMKaW1wb3J0IHBhbmRhcyBhcyBwZAppbXBvcnQgbnVtcHkgYXMgbnAKaW1wb3J0IHlmaW5hbmNlIGFzIHlmCmZyb20gZGF0ZXRpbWUgaW1wb3J0IGRhdGV0aW1lLCB0aW1lZGVsdGEKaW1wb3J0IHdhcm5pbmdzCndhcm5pbmdzLmZpbHRlcndhcm5pbmdzKCdpZ25vcmUnKQo="
target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sl_comparison_analysis.py")
with open(target, "w") as f:
    f.write(base64.b64decode(data).decode())
print("Written to", target)
