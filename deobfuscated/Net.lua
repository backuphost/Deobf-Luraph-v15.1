-- Net (ModuleScript ReplicatedStorage.Packages.Net) -- recovered from a Luraph v15.1 build.
--
-- How: the bootstrap's environment checks were passed in an emulated Roblox environment, the
-- payload was decrypted with the resulting key, run to the `return` of the module's API table,
-- and the script's own code was lifted out of the VM.  Two traced runs were combined: one as the
-- server (fills the server branches) and one as a client (fills the first client guard, lines 5-16).
--
-- Recovered: the whole module body - the client/server split, the "EventServiceLTM" attribute
-- guard, the GetFullName() check, SHA-256, the remote-name scrambling keyed on game.JobId, the
-- anti-exploit checks (reports through "RE/<name>":FireServer and
-- ServerScriptService.Security.OnDetected:RemoveFromGame(player, "X-39", name, 120, 360)), and
-- the returned API (RemoteEvent / UnreliableRemoteEvent / RemoteFunction, Connect,
-- ConnectUnreliable, Handle, Invoke, Clean, Scramble).
--
-- Not recovered / read with care (this is analysis output, not drop-in source; it parses but
-- was not run against the original):
--   * 6 places show `x[nil](...)` / `v26(nil)` / `getfenv()[nil]`: constants of anti-exploit
--     branches that never ran in either run (Luraph decrypts constants only when their
--     instruction executes).  By their neighbours they are `:FireServer(v26("<code>") ..)`
--     reports and a `print`-like global, but the report codes themselves are unknown.
--   * 2 "LIFT ERROR" lines: one in the path-mismatch branch at the top (by shape the same
--     `return { RemoteFunction = function() end, RemoteEvent = function() end }` as the branch
--     above it) and one in the `getrenv` detection branch.
--   * 3 loops marked "bounds not recovered".
--   * "tamper response (never taken in the traced run)" marks Luraph's own key checks, not
--     script logic.
--   * `table.pack(select(n, ...))` / `unpack(v, 1, v.n)` is the VM's handling of `...` and
--     multiple results; variable names are generated (the original local names are not stored).

local v1 = table.pack(select(1, nil --[[multi_args]]))
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
if RunService:IsClient() then
	if workspace:GetAttribute("EventServiceLTM") then
		return {
			RemoteFunction = function()

		end,
			RemoteEvent = function()

		end,
		}
	end
	workspace:SetAttribute("EventServiceLTM", true)
	if script:GetFullName() ~= "ReplicatedStorage.Packages.Net" then
		({}).RemoteFunction = function()

		end
		-- LIFT ERROR: call of None
		return
	end
else
	script = ReplicatedStorage.Packages.Net
end
local v4 = string.rep("%08x", 8)
local v5 = tick()
local v6 = {}
local v7 = buffer.create(256)
for key, value in ipairs({
	1116352408,
	1899447441,
	3049323471,
	3921009573,
	961987163,
	1508970993,
	2453635748,
	2870763221,
	3624381080,
	310598401,
	607225278,
	1426881987,
	1925078388,
	2162078206,
	2614888103,
	3248222580,
	3835390401,
	4022224774,
	264347078,
	604807628,
	770255983,
	1249150122,
	1555081692,
	1996064986,
	2554220882,
	2821834349,
	2952996808,
	3210313671,
	3336571891,
	3584528711,
	113926993,
	338241895,
	666307205,
	773529912,
	1294757372,
	1396182291,
	1695183700,
	1986661051,
	2177026350,
	2456956037,
	2730485921,
	2820302411,
	3259730800,
	3345764771,
	3516065817,
	3600352804,
	4094571909,
	275423344,
	430227734,
	506948616,
	659060556,
	883997877,
	958139571,
	1322822218,
	1537002063,
	1747873779,
	1955562222,
	2024104815,
	2227730452,
	2361852424,
	2428436474,
	2756734187,
	3204031479,
	3329325298,
}) do
	buffer.writeu32(v7, (key - 1) * 4, value)
end
local v13 = function(p1)
	local v34 = buffer.len(p1)
	local v35 = -(v34 + 9) % 64
	local v36 = v34 + 1 + v35 + 8
	local v37 = buffer.create(v36)
	buffer.copy(v37, 0, p1)
	buffer.writeu8(v37, v34, 128)
	local v38 = v34 * 8
	for i = 7, 0, -1 do
		local v40 = v38 % 256
		buffer.writeu8(v37, i + v34 + 1 + v35, v40)
		v38 = (v38 - v40) / 256
	end
	return v37, v36
end
local v14 = buffer.create(256)
local v15 = 156595870
local v16 = function(p2, p3)
	if v15 == 211083613 then
		-- tamper response (never taken in the traced run)
		return
	end
	if v15 == 507832368 then
		-- tamper response (never taken in the traced run)
		return
	end
	local v41 = p3 - 1
	if bit32.bxor(v15, 39800618) == 504094197 then
		-- tamper response (never taken in the traced run)
		return
	end
	local v42 = 1779033703
	local v43 = 3144134277
	local v44 = 1013904242
	local v45 = 2773480762
	local v46 = 1359893119
	local v47 = 2600822924
	local v48 = 528734635
	local v49 = 1541459225
	for i6 = v41, 64, nil do
		local v53, v54, v55, v56, v57
		for i7 = 0, 60, 4 do
			v53 = buffer.writeu32
			v54 = bit32.byteswap
			v55 = table.pack(buffer.readu32(p2, i6 + i7))
			v56 = table.pack(v54(unpack(v55, 1, v55.n)))
			v57 = v56.n
			v56.n = 3 + v57 - 1
			table.move(v56, 1, v57, 3, v56)
			v56[1] = v14
			v56[2] = i7
			v53(unpack(v56, 1, v56.n))
		end
		local v59, v60, v61, v62, v63, v64, v65, v66, v67, v68, v69, v70, v71
		for i8 = 64, 252, 4 do
			v59 = buffer.readu32(v14, i8 - 60)
			v60 = bit32.bxor
			v61 = bit32.rrotate(v59, 7)
			v62 = bit32.rrotate(v59, 18)
			v63 = table.pack(bit32.rshift(v59, 3))
			v64 = v63.n
			v63.n = 3 + v64 - 1
			table.move(v63, 1, v64, 3, v63)
			v63[1] = v61
			v63[2] = v62
			v65 = v60(unpack(v63, 1, v63.n))
			v66 = buffer.readu32(v14, i8 - 8)
			v67 = bit32.bxor
			v68 = bit32.rrotate(v66, 17)
			v69 = bit32.rrotate(v66, 19)
			v70 = table.pack(bit32.rshift(v66, 10))
			v71 = v70.n
			v70.n = 3 + v71 - 1
			table.move(v70, 1, v71, 3, v70)
			v70[1] = v68
			v70[2] = v69
			buffer.writeu32(v14, i8, buffer.readu32(v14, i8 - 64) + v65 + buffer.readu32(v14, i8 - 28) + v67(unpack(v70, 1, v70.n)))
		end
		local v72 = v46
		local v73 = v47
		local v74 = v48
		local v75 = v49
		local v76 = v45
		local v77 = v44
		local v78 = v42
		local v79 = v43
		local v81, v82, v83, v84, v85, v86, v87, v88, v89, v90, v91, v92, v93, v94, v95, v96, v97, v98, v99, v100, v101, v102, v103
		for i9 = 0, 252, 4 do
			v81 = bit32.bxor
			v82 = bit32.rrotate(v72, 6)
			v83 = bit32.rrotate(v72, 11)
			v84 = table.pack(bit32.rrotate(v72, 25))
			v85 = v84.n
			v84.n = 3 + v85 - 1
			table.move(v84, 1, v85, 3, v84)
			v84[1] = v82
			v84[2] = v83
			v86 = v81(unpack(v84, 1, v84.n))
			v87 = bit32.bxor
			v88 = bit32.band(v72, v73)
			v89 = table.pack(bit32.band(bit32.bnot(v72), v74))
			v90 = v89.n
			v89.n = 2 + v90 - 1
			table.move(v89, 1, v90, 2, v89)
			v89[1] = v88
			v91 = v75 + v86 + v87(unpack(v89, 1, v89.n)) + buffer.readu32(v7, i9) + buffer.readu32(v14, i9)
			v92 = v76 + v91
			v93 = bit32.bxor
			v94 = bit32.rrotate(v78, 2)
			v95 = bit32.rrotate(v78, 13)
			v96 = table.pack(bit32.rrotate(v78, 22))
			v97 = v96.n
			v96.n = 3 + v97 - 1
			table.move(v96, 1, v97, 3, v96)
			v96[1] = v94
			v96[2] = v95
			v98 = v93(unpack(v96, 1, v96.n))
			v99 = bit32.bxor
			v100 = bit32.band(v78, v79)
			v101 = bit32.band(v78, v77)
			v102 = table.pack(bit32.band(v79, v77))
			v103 = v102.n
			v102.n = 3 + v103 - 1
			table.move(v102, 1, v103, 3, v102)
			v102[1] = v100
			v102[2] = v101
			v75 = v74
			v76 = v77
			v74 = v73
			v77 = v79
			v73 = v72
			v79 = v78
			v72 = v92
			v78 = v91 + v98 + v99(unpack(v102, 1, v102.n))
		end
		v42 = bit32.bor(v78 + v42, 0)
		v43 = bit32.bor(v79 + v43, 0)
		v44 = bit32.bor(v77 + v44, 0)
		v45 = bit32.bor(v76 + v45, 0)
		v46 = bit32.bor(v72 + v46, 0)
		v47 = bit32.bor(v73 + v47, 0)
		v48 = bit32.bor(v74 + v48, 0)
		v49 = bit32.bor(v75 + v49, 0)
		-- for-loop without visible setup
		for i6 = nil --[[bounds not recovered]], nil do
			for i7 = 0, 60, 4 do
				v53 = buffer.writeu32
				v54 = bit32.byteswap
				v55 = table.pack(buffer.readu32(p2, i6 + i7))
				v56 = table.pack(v54(unpack(v55, 1, v55.n)))
				v57 = v56.n
				v56.n = 3 + v57 - 1
				table.move(v56, 1, v57, 3, v56)
				v56[1] = v14
				v56[2] = i7
				v53(unpack(v56, 1, v56.n))
			end
			for i8 = 64, 252, 4 do
				v59 = buffer.readu32(v14, i8 - 60)
				v60 = bit32.bxor
				v61 = bit32.rrotate(v59, 7)
				v62 = bit32.rrotate(v59, 18)
				v63 = table.pack(bit32.rshift(v59, 3))
				v64 = v63.n
				v63.n = 3 + v64 - 1
				table.move(v63, 1, v64, 3, v63)
				v63[1] = v61
				v63[2] = v62
				v65 = v60(unpack(v63, 1, v63.n))
				v66 = buffer.readu32(v14, i8 - 8)
				v67 = bit32.bxor
				v68 = bit32.rrotate(v66, 17)
				v69 = bit32.rrotate(v66, 19)
				v70 = table.pack(bit32.rshift(v66, 10))
				v71 = v70.n
				v70.n = 3 + v71 - 1
				table.move(v70, 1, v71, 3, v70)
				v70[1] = v68
				v70[2] = v69
				buffer.writeu32(v14, i8, buffer.readu32(v14, i8 - 64) + v65 + buffer.readu32(v14, i8 - 28) + v67(unpack(v70, 1, v70.n)))
			end
			v72 = v46
			v73 = v47
			v74 = v48
			v75 = v49
			v76 = v45
			v77 = v44
			v78 = v42
			v79 = v43
			for i9 = 0, 252, 4 do
				v81 = bit32.bxor
				v82 = bit32.rrotate(v72, 6)
				v83 = bit32.rrotate(v72, 11)
				v84 = table.pack(bit32.rrotate(v72, 25))
				v85 = v84.n
				v84.n = 3 + v85 - 1
				table.move(v84, 1, v85, 3, v84)
				v84[1] = v82
				v84[2] = v83
				v86 = v81(unpack(v84, 1, v84.n))
				v87 = bit32.bxor
				v88 = bit32.band(v72, v73)
				v89 = table.pack(bit32.band(bit32.bnot(v72), v74))
				v90 = v89.n
				v89.n = 2 + v90 - 1
				table.move(v89, 1, v90, 2, v89)
				v89[1] = v88
				v91 = v75 + v86 + v87(unpack(v89, 1, v89.n)) + buffer.readu32(v7, i9) + buffer.readu32(v14, i9)
				v92 = v76 + v91
				v93 = bit32.bxor
				v94 = bit32.rrotate(v78, 2)
				v95 = bit32.rrotate(v78, 13)
				v96 = table.pack(bit32.rrotate(v78, 22))
				v97 = v96.n
				v96.n = 3 + v97 - 1
				table.move(v96, 1, v97, 3, v96)
				v96[1] = v94
				v96[2] = v95
				v98 = v93(unpack(v96, 1, v96.n))
				v99 = bit32.bxor
				v100 = bit32.band(v78, v79)
				v101 = bit32.band(v78, v77)
				v102 = table.pack(bit32.band(v79, v77))
				v103 = v102.n
				v102.n = 3 + v103 - 1
				table.move(v102, 1, v103, 3, v102)
				v102[1] = v100
				v102[2] = v101
				v75 = v74
				v76 = v77
				v74 = v73
				v77 = v79
				v73 = v72
				v79 = v78
				v72 = v92
				v78 = v91 + v98 + v99(unpack(v102, 1, v102.n))
			end
		end
		break
	end
	return v42, v43, v44, v45, v46, v47, v48, v49
end
v15 = bit32.bxor(v15, 83906104)
local v17 = function(p4, p5)
	if p5 and 0 < buffer.len(p5) then
		local v104 = buffer.create(buffer.len(p4) + buffer.len(p5))
		buffer.copy(v104, 0, p4)
		buffer.copy(v104, buffer.len(p4), p5)
		p4 = v104
	end
	local v105, v106 = v13(p4)
	local v107, v108, v109, v110, v111, v112, v113, v114 = v16(v105, v106)
	return string.format(v4, v107, v108, v109, v110, v111, v112, v113, v114)
end
local v18 = {}
local isServer = RunService:IsServer()
local v20
if RunService:IsStudio() then
	v20 = "00000000-0000-0000-0000-000000000000"
else
	v20 = game.JobId
end
local v21 = v20
local v23 = nil
local v24 = 74383903
local v25 = function(p6, p7)
	local v115 = (p6 - 32 + p7) % 95
	if bit32.bxor(v24, 210676092) ~= 114950760 then
		v24 = bit32.bxor(v24, 3006071745)
	end
	return v115 + 32
end
v24 = 173320980
local v26 = function(p8)
	local pack = table.pack
	local v117 = table.pack(string.byte(v21, 1, -1))
	local v118 = pack(unpack(v117, 1, v117.n))
	local v119 = ""
	local v120 = 1
	for i10 = #p8, 1, -1 do
		local char = string.char
		local v123 = table.pack(v25(string.byte(p8, i10), v118[v120]))
		v119 ..= char(unpack(v123, 1, v123.n))
		v120 = v120 % v118.n + 1
	end
	return v119
end
if isServer then
	v18.Scramble = v26
end
local v27 = pcall
local v28 = debug.info(getfenv, "slfna")
local v29 = function(p9, p10, p11)
	if v27 ~= pcall then
		local re = script:WaitForChild("RE/" .. v23, 10)
		re[nil](re, v26(nil) .. utf8.char(65279), 1)
		return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
	end
	local ok, result = pcall(error, "bahah")
	if ok or not result or result ~= "bahah" then
		local re2 = script:WaitForChild("RE/" .. v23, 10)
		re2[nil](re2, v26(nil) .. utf8.char(65279), 1)
		return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
	end
	if debug.info(getfenv, "slfna") ~= v28 then
		script:WaitForChild("RE/" .. v23, 10):FireServer(v26("54HHF8463") .. utf8.char(65279), 1)
		return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
	end
	if debug.getmemorycategory() == "" or debug.getmemorycategory() == "--" then
		script:WaitForChild("RE/" .. v23, 10):FireServer(v26("49ajj4") .. utf8.char(65279), 1)
		return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
	end
	local v126 = tick() - v5
	local v132
	repeat
		local exit4
		repeat
			if v126 > 10 then
				if v126 ~= v126 then
					exit4 = true
					break
				end
				for i11 = 1, 10 do
					local ok2, result2 = pcall(getfenv, i11)
					local v141 = ok2
					repeat
						local exit2, exit3
						repeat
							local exit1
							repeat
								if shared.AC489asdAK then
									warn("transform", v141)
									if v141 and result2 then
										table.foreach(result2, print)
										if v141 then
											exit1 = true
											break
										end
										exit2 = true
										break
									end
								end
							until true
							if exit1 then
								break
							end
							if exit2 then
								break
							end
							if not v141 then
								exit3 = true
								break
							end
						until true
						if exit2 then
							break
						end
						if exit3 then
							break
						end
						if result2.cloneref then
							local re3 = script:WaitForChild("RE/" .. v23, 10)
							re3[nil](re3, v26(nil) .. utf8.char(65279), 1)
							return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
						end
					until true
					if v141 == false and result2 == "attempt to call a nil value" then
						script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
						return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
					end
					if v141 and result2.getrenv then
						local v142 = script:WaitForChild("RE/" .. v23, 10)[nil]
						local v143 = v26(nil)
						-- LIFT ERROR: register 6064 out of range (bad self-modifying patch?)
						return
					end
					if v141 and result2.getupvalues then
						script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
						return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
					end
					if v141 and result2.getnamecallmethod then
						script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
						return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
					end
					if v141 and result2.superflow_bytecode then
						script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
						return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
					end
				end
			end
		until true
		if exit4 then
			break
		end
		if not p11 then
			p11 = "/"
		end
		local pack2 = table.pack
		local v129 = table.pack(string.byte(p10, 1, -1))
		local v130 = pack2(unpack(v129, 1, v129.n))
		local n = v130.n
		v132 = ""
		local v133 = 1
		for i14 = #p9, 1, -1 do
			while true do
				local v135 = string.sub(p9, i14, i14)
				if v135 == p11 then
					v132 ..= v135
					-- for-loop without visible setup
					local exit6
					for i14 = nil --[[bounds not recovered]], nil do
						exit6 = true
						break
					end
					if exit6 then
						continue
					end
					break
				end
				local v136 = string.byte(v135)
				local char2 = string.char
				local v138 = table.pack(v25(v136, v130[v133]))
				v132 ..= char2(unpack(v138, 1, v138.n))
				v133 = v133 % n + 1
				-- for-loop without visible setup
				local exit5
				for i14 = nil --[[bounds not recovered]], nil do
					exit5 = true
					break
				end
				if exit5 then
					continue
				end
				break
			end
			break
		end
	until true
	return v132
end
getfenv().UGCClothesStorage = "2b857474-f9cc-4ddd-a4f1-28be8b6c1b38"
local v30 = function(p12)
	for i15 = 1, 10 do
		local ok3, result3 = pcall(getfenv, i15)
		local v155 = ok3
		if shared.AC489asdAK then
			warn(v155)
			if v155 and result3 then
				table.foreach(result3, getfenv()[nil])
				local script2 = result3.script
				local g = result3._G
				local shared2 = result3.shared
				local v160 = table.pack(rawget(result3, "shared"))
				while true do
				end
			end
		end
		if result3 and result3.script and result3._G and result3.shared and not rawget(result3, "_G") and not rawget(result3, "shared") then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("KJHAFGJKH9") .. utf8.char(65279), 1)
			return "068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 and result3.cloneref then
			local re4 = script:WaitForChild("RE/" .. v23, 10)
			re4[nil](re4, v26(nil) .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 == false and result3 == "attempt to call a nil value" then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 and result3.getrenv then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 and result3.getupvalues then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 and result3.getnamecallmethod then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if v155 and result3.superflow_bytecode then
			script:WaitForChild("RE/" .. v23, 10):FireServer(v26("StopTrying") .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
	end
	local fromstring = buffer.fromstring
	local v149 = table.pack(v29(p12, v21))
	local v150 = fromstring(unpack(v149, 1, v149.n))
	local v151 = table.pack(buffer.fromstring(getfenv().UGCClothesStorage .. v21))
	local v152 = table.pack(v150, unpack(v151, 1, v151.n))
	return v17(unpack(v152, 1, v152.n))
end
local v31 = 3
v23 = v30("InventoryService/Sort")
v18.UnreliableRemoteEvent = function(p13, p14)
	local v161 = "URE/" .. p14
	local v162 = v30
	local v163 = "URE/" .. v30(p14)
	if not isServer then
		local v164 = script:WaitForChild(v163, 10)
		if v164 then
			return v164
		end
		error(nil .. v163, 2)
		return v164
	end
	if not script:FindFirstChild(v161) and not table.find(v6, v161) then
		task.delay(math.random(0.1, 3), function()
			local unreliableRemoteEvent = Instance.new("UnreliableRemoteEvent")
			unreliableRemoteEvent.Name = v161
			unreliableRemoteEvent.Parent = script
			unreliableRemoteEvent.OnServerEvent:Connect(function(p15)
				require(game:GetService("ServerScriptService").Security.OnDetected):RemoveFromGame(p15, "X-39", v161, 120, 360)
			end)
		end)
	end
	local v165 = script:FindFirstChild(v163)
	if not v165 then
		local new = Instance.new
		if v162 then
			local v167 = new("UnreliableRemoteEvent")
			v167.Name = v163
			v167.Parent = script
			return v167
		end
	end
	return v165
end
v18.RemoteEvent = function(p16, p17)
	local v169 = "RE/" .. p17
	local v170 = v30
	local v171 = "RE/" .. v30(p17)
	if not isServer then
		local v172 = script:WaitForChild(v171, 10)
		if v172 then
			return v172
		end
		error("Failed to find RemoteEvent: " .. v171, 2)
		return v172
	end
	if not script:FindFirstChild(v169) and not table.find(v6, v169) then
		task.delay(math.random(0.1, 3), function()
			local remoteEvent = Instance.new("RemoteEvent")
			remoteEvent.Name = v169
			remoteEvent.Parent = script
			remoteEvent.OnServerEvent:Connect(function(p18)
				require(game:GetService("ServerScriptService").Security.OnDetected):RemoveFromGame(p18, "X-39", v169, 120, 360)
			end)
		end)
	end
	local v173 = script:FindFirstChild(v171)
	if not v170 or v173 then
		return v173
	end
	local remoteEvent2 = Instance.new("RemoteEvent")
	remoteEvent2.Name = v171
	remoteEvent2.Parent = script
	return remoteEvent2
end
v18.ConnectUnreliable = function(p19, p20, p21)
	if isServer then
		return p19:UnreliableRemoteEvent(p20).OnServerEvent:Connect(p21)
	end
	return p19:UnreliableRemoteEvent(p20).OnClientEvent:Connect(p21)
end
local v32 = 172419375
local v33 = function(p22, p23, p24)
	local v176 = isServer
	if bit32.bxor(v32, 134324858) ~= 45014948 then
		-- tamper response (never taken in the traced run)
		return
	end
	if v176 then
		return p22:RemoteEvent(p23).OnServerEvent:Connect(p24)
	end
	return p22:RemoteEvent(p23).OnClientEvent:Connect(p24)
end
v32 = 179273182
v18.Connect = v33
v18.RemoteFunction = function(p25, p26)
	local v177 = "RF/" .. p26
	local v178 = "RF/" .. v30(p26)
	if not isServer then
		local v179 = script:WaitForChild(v178, 10)
		if v179 then
			return v179
		end
		error(nil .. v178, 2)
		return v179
	end
	if not script:FindFirstChild(v177) and not table.find(v6, v177) then
		task.delay(math.random(0.1, 3), function()
			local remoteFunction = Instance.new("RemoteFunction")
			remoteFunction.Name = v177
			remoteFunction.Parent = script
			remoteFunction.OnServerInvoke = function(p27)
				require(game:GetService("ServerScriptService").Security.OnDetected):RemoveFromGame(p27, "X-39", v177, 120, 360)
			end
		end)
	end
	local v180 = script:FindFirstChild(v178)
	if v180 then
		return v180
	end
	local remoteFunction2 = Instance.new("RemoteFunction")
	remoteFunction2.Name = v178
	remoteFunction2.Parent = script
	return remoteFunction2
end
v18.Handle = function(p28, p29, p30)
	p28:RemoteFunction(p29).OnServerInvoke = p30
end
v18.Invoke = function(p31, p32)
	local v183 = table.pack(select(3, nil --[[multi_args]]))
	local v184 = p31:RemoteFunction(p32)
	local invokeServer = v184.InvokeServer
	local v186 = table.pack(unpack(v183, 1, v183.n))
	local v187 = table.pack(v184, unpack(v186, 1, v186.n))
	return invokeServer(unpack(v187, 1, v187.n))
end
v18.Clean = function(p33)
	script:ClearAllChildren()
end
return v18
