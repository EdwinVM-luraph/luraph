-- This file was protected using Luraph Obfuscator v15.0 [https://lura.ph/]
-- Synthetic shared closure frame: reads and writes must reach the same local.
local env = {
    [1] = function(captures)
        return function(value)
            if value ~= nil then captures[1][5][captures[1][4]] = value end
            return captures[1][5][captures[1][4]]
        end
    end,
}
local function make(env, proto, upvalues)
    local ops = proto[1]
    local closure
    closure = function(...)
        local pc = 1
        local registers = table.create(3)
        local open = {}
        while true do
            local op = ops[pc]
            if op == 1 then
                registers[1] = 11
            elseif op == 2 then
                open[1] = {[5] = registers, [4] = 1}
                registers[2] = env[1]({open[1]})
            elseif op == 3 then
                registers[1] = 23
                print(registers[2]())
            elseif op == 4 then
                print(registers[2](42))
                print(registers[1])
            else
                open[1] = nil
                -- Unreachable generic close handler identifies the open-box table.
                if false then for k, v in next, open do open[k] = nil end end
                return registers[1]
            end
            pc = pc + 1
        end
    end
    return closure
end
local main = make(env, {{1, 2, 3, 4, 5}}, {})
main()
