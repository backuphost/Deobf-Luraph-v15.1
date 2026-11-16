-- loadstring6 (ZekeHub / Luarmor loader) -- the part of the script that ran, lifted out of its Luraph VM.  PARTIAL.
--
-- What happens when the file runs (traced in an emulated Roblox environment):
--   1. Luraph's bootstrap (36 functions) checks the environment and decrypts the script.  It calls a global
--      `math_floor_precision` six times (not a Roblox or executor function; an earlier loader stage must
--      define it; the emulation answers with the argument itself).
--   2. The script (below) builds a keystream table, decrypts its strings and calls
--      game:HttpGet("https://backend.zekehub.com/luarmor/precheck").
--   3. The reply is used as a number in the key of what comes next.  With no reply the script stops
--      (arithmetic on nil); with a made-up reply ("1") it decodes garbage for about 28 million VM instructions
--      and stops with "buffer access out of bounds".  The real reply was not available, so nothing after
--      the precheck is recovered.
--
-- The listing is the keystream / string routine at the start of the script only: the lift stops at
-- instructions that rewrite themselves with run-time values (11 LIFT ERROR marks), so the HttpGet call
-- itself is not in it.  "tamper response" marks Luraph's
-- own key checks.  Made with: python -m devm.run <file> --executor --no-exercise

local v2 = nil
local v4 = nil
local v6 = nil
local v8 = nil
local v10 = nil
local v12 = nil
if p3 == 2 then
	-- tamper response (never taken in the traced run)
	return
end
local v13 = {}
local v15, v45
repeat
	local exit1, v14, v16, v35, v107, v108, v109
	repeat
		if p3 == 1 then
			v14 = v13
			v15 = {}
			v6 = {}
			v16 = p1 == 85854657
			if not v16 then
				local v17 = 2656711301 - p1
				local v18 = "9"
				local v19 = "0"
				local v20 = "3"
				local v21 = "a"
				local v22 = "c"
				local v23 = bit32.band(p1, 4294967295)
				local v24 = bit32.band(v23, 65535)
				local v25 = bit32.band(41579 * v24 + bit32.lshift(bit32.band(41579 * bit32.rshift(v23, 16) + 12253 * v24, 65535), 16), 4294967295) % 4294967296
				if v25 == v25 then
					local v26 = v25 - v25 % 1
				end
				v6[1] = v18
				v6[2] = v19
				v6[3] = v20
				v6[4] = v21
				v6[5] = v22
				v6[6] = "f"
				v6[7] = "d"
				v6[8] = "2"
				v6[9] = "7"
				v6[10] = "5"
				v6[11] = "4"
				v6[12] = "e"
				v6[13] = "6"
				v6[14] = "8"
				v6[15] = "1"
				v6[16] = "b"
				-- LIFT ERROR: self-modifying store of a run-time value into __LPH_PROTOS[37]["vars"]["A"][-2][784]
				return
			end
			v15[p2] = (v14 - v16) / v8
			v108 = (v107 - 3541900433) % 4294967296
			if v108 == v108 then
				v109 = v108 - v108 % 1
			end
		else
			v15 = v13
			local v29 = bit32.band(p1, 4294967295)
			local v30 = bit32.band(v29, 65535)
			local v31 = bit32.band(41579 * v30 + bit32.lshift(bit32.band(41579 * bit32.rshift(v29, 16) + 12253 * v30, 65535), 16), 4294967295) % 4294967296
			if v31 == v31 then
				local v32 = v31 - v31 % 1
			end
			if p3 ~= 0 then
				exit1 = true
				break
			end
			v14 = 2024849952699127
			v2 = 1704967294960101
			v4 = 4503599627370496
			v6 = 67108864
			v8 = 17592186044416
			v10 = 19018253
			v12 = 20882157
			for i = 1, 4 do
				local v110 = p2 % 65536
				if v110 < 0 then
					v110 += 65536
				end
				if v110 >= 65536 then
					v110 -= 65536
				end
				p2 = (p2 - v110) / 65536
				local v111 = (v14 + v110) % v4
				local v112 = v111 % v6
				v14 = ((((v111 - v112) / v6 * v10 + v112 * v12) % v6 * v6 + v112 * v10) % v4 + v2) % v4
			end
			local v34 = (p1 + 3524298472) % 4294967296
			if v34 == v34 then
				p1 = v34 - v34 % 1
			else
				p1 = 0
			end
			v35 = { 1, nil, 32, 0, nil }
		end
	until true
	if exit1 then
		break
	end
	while true do
		local v36 = v35[1]
		local v37 = v35[3]
		local v38 = v35[4] + v36
		local v39 = v36 <= 0
		local v40 = not v39
		local v41 = v38 >= v37
		local v42 = v38 <= v37
		local v43 = v39 and v41 or not v39 and v42
		local v44 = not v39 and v42
		v35[4] = v38
		if not v43 then
			break
		end
		p2 = v38
		local v103 = v14 % v6
		local v104 = (v14 - v103) / v6 * v10
		local v105 = (3524298472 + p1) % 4294967296
		local v106
		if v105 == v105 then
			v106 = v105 - v105 % 1
		else
			v106 = 0
		end
		v107 = v106
		v14 = (((v104 + v103 * v12) % v6 * v6 + v103 * v10) % v4 + v2) % v4
		v16 = v14 % v8
		v15[p2] = (v14 - v16) / v8
		v108 = (v107 - 3541900433) % 4294967296
		if v108 == v108 then
			v109 = v108 - v108 % 1
		end
	end
	v45 = v35[2]
	local v46 = (p1 + 770668824) % 4294967296
	if v46 == v46 then
		p1 = v46 - v46 % 1
	else
		p1 = 0
	end
until true
local v47 = v15
local v48 = v47[3]
local v49 = 4 * (v47[1] % 64) + 1
local v50 = (p1 + 2409567758) % 4294967296
local v51
if v50 == v50 then
	v51 = v50 - v50 % 1
else
	v51 = 0
end
if not v47 then
	local v52 = v8 + v4
	local v53 = (v49 - v6) / 2
	local v54 = (v2 - v10) / 2
	local v55 = v53 % 2
	local v56 = v54 % 2
	if v55 ~= v56 then
		v52 += 64
	end
	local v57 = (v53 - v55) / 2
	local v58 = (v54 - v56) / 2
	local v59 = v57 % 2
	local v60 = v58 % 2
	if v59 ~= v60 then
		local v61 = v52 + 128
	end
	local v62 = (v57 - v59) / 2
	local v63 = (v58 - v60) / 2
	-- LIFT ERROR: register index Sym('t', 'L4P1728_2')
	return
end
local v64 = bit32.bxor(2656711301, v51)
local v65 = 2 * (v47[2] % 128) - 1
if v64 + 497926378 == 258278821 then
	-- tamper response (never taken in the traced run)
	return
end
local v66 = 1
local v67 = { nil, v45, 0 - v66, v66, 255 }
local v68 = v67[4]
local v69 = v67[5]
local v70 = v67[3] + v68
local v71 = v68 <= 0
local v72 = not v71
local v73 = v70 >= v69
local v74 = v70 <= v69
local v75 = v71 and v73 or not v71 and v74
local v76 = not v71 and v74
v67[3] = v70
if v75 then
	-- LIFT ERROR: register store index Sym('t', 'L4P2326_24')
	return
end
local v77 = v67[2]
if v51 + 507111316 == 530651761 then
	-- tamper response (never taken in the traced run)
	return
end
local v78 = bit32.bor(803054187, v51)
local v79 = { v77, nil, 1, -1, 255 }
local v80 = v79[3]
local v81 = v79[5]
local v82 = v79[4] + v80
local v83 = v80 <= 0
local v84 = not v83
local v85 = v82 >= v81
local v86 = v82 <= v81
local v87 = v83 and v85 or not v83 and v86
local v88 = not v83 and v86
v79[4] = v82
if v87 then
	-- LIFT ERROR: register index Sym('t', 'L4P1365_2')
	return
end
local v89 = 0
local v90 = ""
local v91 = { nil, v79[1], 1, #p4 + 0, 0 }
local v92 = v91[3]
local v93 = v91[4]
local v94 = v91[5] + v92
local v95 = v92 <= 0
local v96 = not v95
local v97 = v94 >= v93
local v98 = v94 <= v93
local v99 = v95 and v97 or not v95 and v98
local v100 = not v95 and v98
v91[5] = v94
if v99 then
	local v102 = (v89 + 1) % 256
	-- LIFT ERROR: register index Sym('t', 'L4P1410_2')
	return
end
local v101 = v91[2]
p5[p6] = v90
if bit32.bxor(v51, 41326761) ~= 157690642 then
	return
end
-- tamper response (never taken in the traced run)
