import os

db_path = os.path.abspath(".owm/bundles/openworm/c_elegans/2/worm.db")

print(f"--- Database Integrity Check ---")
print(f"Checking: {db_path}")

if os.path.exists(db_path):
    size = os.path.getsize(db_path)
    print(f"✅ File exists.")
    print(f"📊 File size: {size / 1024:.2f} KB")
    
    if size < 50:
        print("❌ ERROR: The database file is almost empty. It likely didn't download correctly.")
    else:
        print("✅ The file size looks promising.")
else:
    print("❌ ERROR: The file 'worm.db' is MISSING from that folder.")

# Let's check for the config file too
conf_path = os.path.abspath(".owm/bundles/openworm/c_elegans/2/bundle.conf")
if os.path.exists(conf_path):
    print("✅ 'bundle.conf' is present.")
else:
    print("❌ 'bundle.conf' is MISSING.")